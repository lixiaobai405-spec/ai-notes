"""Read-only monitoring against the isolated PostgreSQL test database."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.modules.ai.models import AiConversation, AiRun, AiStep
from app.modules.files.models import File, PdfImport, UploadSession
from app.modules.notes.models import Note
from app.shared.db import Base, SessionLocal, engine
from app.shared.models import Job, User

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import monitoring_database as monitor
from scripts.monitoring_common import MonitoringError

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=1)


@pytest.fixture
def rows():
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User.id).where(User.username == "admin"))
        conversation = AiConversation(owner_id=owner, title="private title", request_key=uuid4())
        note = Note(owner_id=owner, title="private note", kind="image")
        db.add_all([conversation, note]); db.flush()
        conversation_id, note_id = conversation.id, note.id

    def add(row_type, **values):
        with SessionLocal.begin() as db:
            row = row_type(**values); db.add(row); db.flush()
            return row.id

    def job(status="queued", created_at=NOW - timedelta(minutes=5), **values):
        defaults = dict(owner_id=owner, kind="image.process", status=status, created_at=created_at,
                        run_after=created_at, dedupe_key=uuid4().hex, payload={"private": "not projected"})
        defaults.update(values)
        return add(Job, **defaults)

    def run(status="succeeded", created_at=NOW - timedelta(minutes=5), **values):
        defaults = dict(owner_id=owner, conversation_id=conversation_id, request_key=uuid4(),
            request_hash="a" * 64, model="private-model", prompt="private prompt", input_messages=[],
            answer="private answer", status=status, created_at=created_at,
            finished_at=NOW if status not in {"queued", "running"} else None)
        defaults.update(values)
        return add(AiRun, **defaults)

    def file(status="ready", created_at=NOW - timedelta(minutes=5), **values):
        defaults = dict(owner_id=owner, object_key=uuid4().hex, filename="private filename",
            mime="image/png", expected_bytes=8, status=status, created_at=created_at)
        defaults.update(values)
        return add(File, **defaults)

    def upload(status="completed", created_at=NOW - timedelta(minutes=5), file_status="ready", **values):
        file_id = file(status=file_status, created_at=values.pop("file_created_at", created_at))
        return add(UploadSession, **dict(owner_id=owner, note_id=note_id, file_id=file_id,
            request_key=uuid4().hex, status=status, expires_at=NOW + timedelta(hours=1),
            created_at=created_at, **values))

    def pdf(status="rejected", created_at=NOW - timedelta(minutes=5)):
        return add(PdfImport, owner_id=owner, note_id=note_id, source_file_id=file(mime="application/pdf"),
                   request_hash="b" * 64, status=status, created_at=created_at)

    return dict(job=job, run=run, file=file, upload=upload, pdf=pdf, owner=owner)


def collect(**values):
    return monitor.collect_database_metrics(engine.url, now=NOW, **values)


def fingerprint():
    # Only database-side digests leave PostgreSQL, never private row material.
    with engine.connect() as connection:
        hashes = [connection.execute(text(f'SELECT md5(COALESCE(string_agg(row_to_json(t)::text, '
                    f"',' ORDER BY row_to_json(t)::text), '')) FROM public.\"{table.name}\" AS t")).scalar_one()
                  for table in Base.metadata.sorted_tables]
    return hashlib.sha256("".join(hashes).encode()).hexdigest()


def test_empty_sets_have_known_zero_counts_and_no_invented_age_or_cost():
    result = collect()
    assert result["jobs"]["status"] == result["ai"]["status"] == "ok"
    assert result["jobs"]["ready_queued"] == result["jobs"]["failed_total"] == 0
    assert result["jobs"]["oldest_ready_seconds"] is None
    assert result["ai"]["oldest_queued_seconds"] is None
    assert result["uploads"]["created_window"] == 0
    assert result["usage"]["known_total_tokens"] == result["usage"]["terminal_runs_window"] == 0
    assert result["usage"]["cost"] is result["uploads"]["http_failure_rate"] is None
    assert result["usage"]["cost_status"] == result["uploads"]["http_failure_rate_status"] == "unavailable"


def test_job_readiness_and_created_failure_window_do_not_claim_failure_event_times(rows):
    rows["job"](created_at=NOW - timedelta(days=3), run_after=NOW - timedelta(seconds=120))
    rows["job"](created_at=NOW - timedelta(days=4), run_after=NOW + timedelta(seconds=30))
    rows["job"](created_at=NOW, run_after=NOW)
    rows["job"]("failed", created_at=SINCE)
    rows["job"]("failed", created_at=NOW)
    rows["job"]("failed", created_at=SINCE - timedelta(microseconds=1))
    rows["job"]("failed", created_at=NOW + timedelta(microseconds=1))
    rows["job"]("running", lease_until=NOW)
    rows["job"]("running", lease_until=NOW + timedelta(seconds=1))
    rows["job"]("running", lease_until=None)
    result = collect()["jobs"]
    assert result["ready_queued"] == 2 and result["scheduled_queued"] == 1
    assert result["oldest_ready_seconds"] == 120
    assert result["failed_total"] == 4 and result["failed_created_window"] == 2
    assert result["running"] == 3 and result["expired_leases"] == 1


def test_ai_errors_use_finished_window_controlled_provider_codes_and_exclude_cancelled(rows):
    rows["run"]("failed", created_at=NOW - timedelta(days=2), finished_at=SINCE, error_code="provider_timeout")
    rows["run"]("interrupted", finished_at=NOW, error_code="worker_interrupted")
    rows["run"]("truncated", error_code="output_limit")
    rows["run"]("cancelled", error_code="provider_timeout")
    rows["run"]("failed", finished_at=SINCE - timedelta(microseconds=1), error_code="provider_balance")
    rows["run"]("failed", finished_at=NOW + timedelta(microseconds=1), error_code="provider_timeout")
    rows["run"]("failed", error_code="private raw provider_error@example.com")
    rows["run"]("failed", error_code="credential_rejected")
    rows["run"]("queued", created_at=NOW - timedelta(seconds=321))
    rows["run"]("running", lease_until=NOW)
    result = collect()["ai"]
    assert result["queued"] == result["running"] == result["expired_leases"] == 1
    assert result["oldest_queued_seconds"] == 321
    assert result["errors_finished_window"] == 5 and result["provider_errors_finished_window"] == 2
    assert "private" not in json.dumps(result)


def test_upload_cohort_excludes_derived_pdf_files_and_labels_processing_failure_cohort(rows):
    rows["upload"](created_at=SINCE, file_status="rejected")
    rows["upload"]("expired", created_at=NOW, file_status="expired")
    rows["upload"](created_at=NOW, file_created_at=SINCE - timedelta(days=1), file_status="rejected")
    rows["upload"](created_at=SINCE - timedelta(microseconds=1), file_status="rejected")
    rows["upload"](created_at=NOW + timedelta(microseconds=1), file_status="rejected")
    # Derived PDF page Files are not upload requests, even when rejected.
    for _ in range(5):
        rows["file"]("rejected")
    rows["job"]("failed", kind="pdf.import", created_at=SINCE)
    rows["job"]("failed", kind="image.process", created_at=NOW)
    rows["job"]("failed", kind="unrelated", created_at=NOW)
    rows["job"]("failed", created_at=SINCE - timedelta(microseconds=1))
    rows["pdf"]()
    result = collect()["uploads"]
    assert result["created_window"] == 3
    assert result["rejected_created_window"] == 2 and result["expired_created_window"] == 1
    assert result["processing_failed_created_window"] == 2 and result["pdf_rejected_created_window"] == 1
    assert result["scope"] == "upload_sessions_created_window"
    assert result["processing_failure_scope"] == "image_process_and_pdf_import_jobs_created_window"
    assert result["http_failure_rate"] is None


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": True}, {"total_tokens": False},
    {"total_tokens": -1}, {"total_tokens": 1_000_000_000_001}, {"total_tokens": "35"},
    {"total_tokens": 1.5}, {"total_tokens": "NaN"}, {"total_tokens": "Infinity"},
    {"total_tokens": []}, [], "secret", 10])
def test_invalid_or_missing_usage_is_unknown_never_zero(rows, usage):
    rows["run"](usage=usage)
    result = collect()["usage"]
    assert result["status"] == "partial" and result["known_total_tokens"] is None
    assert result["terminal_runs_window"] == result["missing_usage_runs"] == result["incomplete_usage_runs"] == 1


def test_usage_partial_known_values_pdf_incomplete_and_steps_never_double_count(rows):
    pdf_run = rows["run"](usage={"total_tokens": 100}, progress={"usage_complete": False})
    with SessionLocal.begin() as db:
        db.add(AiStep(run_id=pdf_run, position=0, kind="read", pages=[1], status="succeeded",
                      answer="private step answer", usage={"total_tokens": 100}))
    rows["run"](usage={"total_tokens": 0}, progress={"usage_complete": True})
    rows["run"](usage={"total_tokens": 12.0})
    rows["run"](usage=None)
    rows["run"](usage={"total_tokens": 999}, finished_at=SINCE - timedelta(microseconds=1))
    rows["run"](usage={"total_tokens": 999}, finished_at=NOW + timedelta(microseconds=1))
    rows["run"]("running", usage={"total_tokens": 999})
    result = collect()["usage"]
    assert result["status"] == "partial" and result["known_total_tokens"] == 112
    assert result["terminal_runs_window"] == 4
    assert result["missing_usage_runs"] == 1 and result["incomplete_usage_runs"] == 2


def test_usage_accepts_finite_upper_boundary_and_terminal_finish_window_edges(rows):
    rows["run"]("cancelled", usage={"total_tokens": 7}, finished_at=SINCE)
    rows["run"](usage={"total_tokens": 1_000_000_000_000}, finished_at=NOW)
    result = collect()["usage"]
    assert result["status"] == "ok" and result["known_total_tokens"] == 1_000_000_000_007
    assert result["terminal_runs_window"] == 2 and result["incomplete_usage_runs"] == 0


def test_collector_is_read_only_disposes_null_pool_and_uses_private_aggregate_projections(rows, monkeypatch):
    rows["run"](usage={"total_tokens": 9}, error_code="private raw error")
    rows["upload"]()
    rows["job"]()
    before, observed, created, closed, disposed = fingerprint(), [], [], [], []
    def instrument(*args, **kwargs):
        assert kwargs["poolclass"] is NullPool and kwargs["hide_parameters"] is True
        assert 0 < kwargs["connect_args"]["connect_timeout"] <= 10
        collector = create_engine(*args, **kwargs); created.append(collector)
        @event.listens_for(collector, "before_cursor_execute")
        def inspect(connection, cursor, statement, parameters, context, many):
            observed.append(statement)
            if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                cursor.execute("SHOW transaction_read_only")
                assert cursor.fetchone()[0] == "on"
        @event.listens_for(collector, "close")
        def connection_closed(connection, record):
            closed.append(True)
        @event.listens_for(collector, "engine_disposed")
        def engine_disposed(discarded):
            disposed.append(True)
        return collector
    monkeypatch.setattr(monitor, "create_engine", instrument)
    result = collect()
    assert before == fingerprint() and created[0].pool.status() == "NullPool"
    assert closed == disposed == [True]
    sql = "\n".join(observed).lower()
    assert "read only" in sql
    for forbidden in ("select *", "ai_steps", "username", "password_hash", "filename", "object_key",
                      "input_messages", "answer", "payload", "ciphertext", "token_hash"):
        assert forbidden not in sql
    serialized = json.dumps(result)
    for forbidden in ("private", str(rows["owner"]), "postgresql", "raw error"):
        assert forbidden not in serialized


def test_query_error_savepoint_keeps_other_groups_available_and_sanitizes_error(monkeypatch):
    monkeypatch.setattr(monitor, "JOBS_SQL", "SELECT private_never_echo_this FROM public.jobs")
    result = collect()
    assert result["status"] == "partial" and result["jobs"]["status"] == "unavailable"
    assert result["jobs"]["ready_queued"] is None
    assert result["ai"]["status"] == result["usage"]["status"] == "ok"
    assert "private_never_echo_this" not in json.dumps(result)


def test_global_connection_scope_counts_client_backends_and_includes_collector():
    result = collect()["connections"]
    assert result["scope"] == "cluster_client_backends_including_collector"
    if result["status"] == "ok":
        assert result["total"] > 0 and result["max_connections"] > 0
        assert result["used_percent"] == round(result["total"] / result["max_connections"] * 100, 2)
    else:
        assert result["total"] is result["used_percent"] is None


@pytest.fixture
def restricted_collector(monkeypatch):
    role = "monitor_test_" + uuid4().hex
    with engine.begin() as connection:
        if not connection.execute(text("SELECT current_setting('is_superuser') = 'on'")).scalar_one():
            pytest.skip("Creating a disposable restricted test role requires isolated test superuser")
        connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        connection.execute(text(f"GRANT SELECT ON public.jobs, public.ai_runs, public.files, "
                                f"public.upload_sessions, public.pdf_imports TO {role}"))
    def restricted(*args, **kwargs):
        collector = create_engine(*args, **kwargs)
        @event.listens_for(collector, "connect")
        def set_role(dbapi, record):
            with dbapi.cursor() as cursor:
                cursor.execute(f"SET ROLE {role}")
            dbapi.commit()
        return collector
    monkeypatch.setattr(monitor, "create_engine", restricted)
    try:
        yield role
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
            connection.execute(text(f"DROP ROLE {role}"))


def test_actual_read_only_role_without_global_stats_still_reads_application_aggregates(restricted_collector):
    before = fingerprint()
    result = collect()
    assert before == fingerprint()
    assert result["status"] == "partial"
    assert result["connections"]["status"] == "unavailable"
    assert result["connections"]["code"] == "database_stats_permission_denied"
    assert result["connections"]["total"] is result["connections"]["max_connections"] is None
    assert all(result[group]["status"] == "ok" for group in ("jobs", "ai", "uploads", "usage"))


def test_table_permission_failure_is_partial_and_cannot_abort_later_groups(restricted_collector):
    with engine.begin() as connection:
        connection.execute(text(f"REVOKE SELECT ON public.jobs FROM {restricted_collector}"))
    result = collect()
    assert result["jobs"]["code"] == result["uploads"]["code"] == "database_permission_denied"
    assert result["jobs"]["failed_total"] is None
    assert result["ai"]["status"] == result["usage"]["status"] == "ok"


def test_statement_timeout_is_bounded_for_all_groups_by_one_deadline(monkeypatch):
    # One deliberately slow aggregate consumes the shared budget. Later groups
    # become unavailable immediately rather than receiving another full budget.
    monkeypatch.setattr(monitor, "JOBS_SQL", "SELECT pg_sleep(10) AS ready_queued")
    started = time.monotonic()
    result = collect(timeout_seconds=3)
    assert time.monotonic() - started < 5
    assert result["jobs"]["code"] == "database_timeout"
    assert result["usage"]["status"] == "unavailable"
    assert result["usage"]["known_total_tokens"] is None


def test_read_only_transaction_rejects_mutation_without_changing_fingerprint(monkeypatch):
    before = fingerprint()
    monkeypatch.setattr(monitor, "JOBS_SQL", "DELETE FROM public.jobs RETURNING 1")
    result = collect()
    assert result["jobs"]["status"] == "unavailable" and before == fingerprint()
    assert result["ai"]["status"] == "ok"


@pytest.mark.parametrize("values,code", [({"window_minutes": True}, "monitoring_window_invalid"),
    ({"window_minutes": 0}, "monitoring_window_invalid"),
    ({"timeout_seconds": float("nan")}, "monitoring_timeout_invalid"),
    ({"timeout_seconds": True}, "monitoring_timeout_invalid"),
    ({"now": datetime(2026, 10, 26)}, "monitoring_time_invalid")])
def test_invalid_parameters_are_controlled_errors(values, code):
    with pytest.raises(MonitoringError) as error:
        monitor.collect_database_metrics(engine.url, **values)
    assert error.value.code == code


def test_invalid_url_never_echoes_credentials():
    with pytest.raises(MonitoringError) as error:
        monitor.collect_database_metrics("not-a-url-private-secret")
    assert str(error.value) == "monitoring_database_invalid"


def test_connection_failure_disposes_engine_and_does_not_echo_private_driver_error(monkeypatch):
    disposed = []
    class Unreachable:
        def connect(self):
            raise DBAPIError("private query", {}, RuntimeError("private secret credential"), False)
        def dispose(self):
            disposed.append(True)
    monkeypatch.setattr(monitor, "create_engine", lambda *args, **kwargs: Unreachable())
    result = collect()
    assert disposed == [True] and result["status"] == "unavailable"
    assert all(result[group]["status"] == "unavailable" for group in monitor.GROUP_FIELDS)
    assert "private" not in json.dumps(result)
