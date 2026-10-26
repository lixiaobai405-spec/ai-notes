"""Private PostgreSQL aggregates, independent of application configuration.

No row identifiers, text, credentials, raw errors, or provider requests leave this
module. Each metric group uses a savepoint so denied/failed queries do not hide
other readable groups. The connection is discarded after every collection.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from scripts.monitoring_common import MonitoringError, require


CONNECTION_PERMISSION_SQL = """
SELECT current_setting('is_superuser') = 'on'
    OR pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS allowed
"""
# PostgreSQL 16 pg_stat_activity includes workers as well as client connections.
# Its restricted columns cannot prove a complete global client-backend count
# without pg_read_all_stats (or superuser); never SET ROLE or GRANT privileges.
# https://www.postgresql.org/docs/16/monitoring-stats.html
# https://www.postgresql.org/docs/16/functions-info.html
CONNECTION_SQL = """
SELECT COUNT(*) FILTER (WHERE backend_type = 'client backend') AS total,
       current_setting('max_connections')::integer AS max_connections
FROM pg_catalog.pg_stat_activity
"""
JOBS_SQL = """
SELECT COUNT(*) FILTER (WHERE status = 'queued' AND run_after <= :now) AS ready_queued,
       COUNT(*) FILTER (WHERE status = 'queued' AND run_after > :now) AS scheduled_queued,
       COUNT(*) FILTER (WHERE status = 'running') AS running,
       COUNT(*) FILTER (WHERE status = 'failed') AS failed_total,
       COUNT(*) FILTER (WHERE status = 'failed' AND created_at >= :since
                       AND created_at <= :now) AS failed_created_window,
       EXTRACT(EPOCH FROM (:now - MIN(GREATEST(created_at, run_after))
           FILTER (WHERE status = 'queued' AND run_after <= :now))) AS oldest_ready_seconds,
       COUNT(*) FILTER (WHERE status = 'running' AND lease_until <= :now) AS expired_leases
FROM public.jobs
"""
AI_SQL = """
SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued,
       COUNT(*) FILTER (WHERE status = 'running') AS running,
       COUNT(*) FILTER (WHERE status IN ('failed','interrupted','truncated')
                       AND finished_at >= :since AND finished_at <= :now) AS errors_finished_window,
       COUNT(*) FILTER (WHERE status IN ('failed','interrupted','truncated')
                       AND finished_at >= :since AND finished_at <= :now
                       AND error_code IN ('credential_rejected','provider_balance',
                           'provider_rate_limited','provider_unavailable','provider_timeout',
                           'provider_invalid_response','provider_incomplete','provider_unreachable'))
           AS provider_errors_finished_window,
       EXTRACT(EPOCH FROM (:now - MIN(created_at) FILTER (WHERE status = 'queued')))
           AS oldest_queued_seconds,
       COUNT(*) FILTER (WHERE status = 'running' AND lease_until <= :now) AS expired_leases
FROM public.ai_runs
"""
UPLOADS_SQL = """
SELECT COUNT(*) AS created_window,
       COUNT(*) FILTER (WHERE upload.status = 'expired') AS expired_created_window,
       COUNT(*) FILTER (WHERE file.status = 'rejected') AS rejected_created_window,
       (SELECT COUNT(*) FROM public.jobs WHERE kind IN ('image.process','pdf.import')
           AND status = 'failed' AND created_at >= :since AND created_at <= :now)
           AS processing_failed_created_window,
       (SELECT COUNT(*) FROM public.pdf_imports WHERE status = 'rejected'
           AND created_at >= :since AND created_at <= :now) AS pdf_rejected_created_window
FROM public.upload_sessions AS upload
LEFT JOIN public.files AS file ON file.id = upload.file_id
WHERE upload.created_at >= :since AND upload.created_at <= :now
"""
# JSONB numbers are finite PostgreSQL numeric values. The CASE guards the cast:
# booleans, strings, null, arrays, fractions, negatives, and >1e12 are not usage.
# Only the already aggregated AiRun is read; AiStep must never be summed again.
USAGE_SQL = """
WITH terminal AS (
    SELECT CASE WHEN jsonb_typeof(usage -> 'total_tokens') = 'number'
                THEN CASE WHEN (usage ->> 'total_tokens')::numeric BETWEEN 0 AND 1000000000000
                               AND trunc((usage ->> 'total_tokens')::numeric) = (usage ->> 'total_tokens')::numeric
                          THEN (usage ->> 'total_tokens')::numeric END END AS tokens,
           COALESCE(progress -> 'usage_complete' = 'false'::jsonb, false) AS explicitly_incomplete
    FROM public.ai_runs
    WHERE status IN ('succeeded','cancelled','interrupted','failed','truncated')
          AND finished_at >= :since AND finished_at <= :now
)
SELECT SUM(tokens) AS known_total_tokens, COUNT(*) AS terminal_runs_window,
       COUNT(*) FILTER (WHERE tokens IS NULL) AS missing_usage_runs,
       COUNT(*) FILTER (WHERE tokens IS NULL OR explicitly_incomplete) AS incomplete_usage_runs
FROM terminal
"""

GROUP_FIELDS = {
    "connections": ("total", "max_connections", "used_percent"),
    "jobs": ("ready_queued", "scheduled_queued", "running", "failed_total",
             "failed_created_window", "oldest_ready_seconds", "expired_leases"),
    "ai": ("queued", "running", "errors_finished_window", "provider_errors_finished_window",
           "oldest_queued_seconds", "expired_leases"),
    "uploads": ("created_window", "expired_created_window", "rejected_created_window",
                "processing_failed_created_window", "pdf_rejected_created_window"),
    "usage": ("known_total_tokens", "terminal_runs_window", "missing_usage_runs", "incomplete_usage_runs"),
}


def _unavailable(group, code):
    return {"status": "unavailable", "code": code, **{key: None for key in GROUP_FIELDS[group]}}


def _error_code(error):
    # Neither str(error), SQL text, nor arbitrary DB/application error codes may
    # cross the monitoring boundary. Only fixed diagnostic categories are public.
    if isinstance(error, MonitoringError):
        return error.code if error.code == "database_timeout" else "database_stats_permission_denied"
    state = getattr(getattr(error, "orig", None), "sqlstate", None)
    return {"42501": "database_permission_denied", "57014": "database_timeout",
            "55P03": "database_timeout"}.get(state, "database_query_unavailable")


def _number(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _remaining_ms(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MonitoringError("database_timeout")
    return max(1, math.floor(remaining * 1000))


def _set_timeout(connection, deadline):
    remaining_ms = _remaining_ms(deadline)
    # Values originate solely from the monotonic deadline, never a URL or SQL
    # payload. Apply both limits because a read can wait for a schema/table lock.
    connection.execute(text(f"SET LOCAL statement_timeout = {remaining_ms}"))
    connection.execute(text(f"SET LOCAL lock_timeout = {remaining_ms}"))


def _group(connection, name, sql, parameters, deadline):
    try:
        _set_timeout(connection, deadline)
        with connection.begin_nested():
            if name == "connections":
                _set_timeout(connection, deadline)
                allowed = connection.execute(text(CONNECTION_PERMISSION_SQL)).scalar_one()
                if not allowed:
                    raise MonitoringError("database_stats_permission_denied")
            _set_timeout(connection, deadline)
            row = connection.execute(text(sql), parameters).mappings().one()
            result = {"status": "ok", **{key: _number(value) for key, value in row.items()}}
            if name == "connections":
                result["used_percent"] = round(result["total"] * 100 / result["max_connections"], 2)
            for key in ("oldest_ready_seconds", "oldest_queued_seconds"):
                if key in result and result[key] is not None:
                    result[key] = max(0, result[key])
            if name == "usage":
                if result["terminal_runs_window"] == 0:
                    result["known_total_tokens"] = 0
                if result["incomplete_usage_runs"]:
                    result["status"] = "partial"
            return result
    except (SQLAlchemyError, MonitoringError) as error:
        return _unavailable(name, _error_code(error))


def collect_database_metrics(database_url, *, window_minutes=60, timeout_seconds=10, now=None):
    """Return bounded, read-only aggregates without loading app settings/engine.

    Creation/finish windows include both endpoints. An empty age has no value;
    an empty terminal window has zero known tokens; nonempty unknown usage has
    no known total. Financial cost and HTTP failure rates need other sources.
    """
    require(type(window_minutes) is int and 1 <= window_minutes <= 10080, "monitoring_window_invalid")
    # libpq has an effective minimum connect timeout of two seconds. This
    # deadline bounds the remaining SQL budget, not a subprocess hard kill of
    # DNS/libpq. The caller/service must enforce its own outer wall-clock limit.
    require(type(timeout_seconds) is int and 2 <= timeout_seconds <= 60, "monitoring_timeout_invalid")
    now = datetime.now(timezone.utc) if now is None else now
    require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
            "monitoring_time_invalid")
    now = now.astimezone(timezone.utc)
    try:
        parsed = make_url(database_url)
        require(parsed.drivername in {"postgresql", "postgresql+psycopg"} and bool(parsed.database),
                "monitoring_database_invalid")
    except (ValueError, TypeError, SQLAlchemyError):
        raise MonitoringError("monitoring_database_invalid") from None
    result = {name: _unavailable(name, "database_unavailable") for name in GROUP_FIELDS}
    parameters = {"now": now, "since": now - timedelta(minutes=window_minutes)}
    collector_engine = None
    deadline = time.monotonic() + timeout_seconds
    try:
        timeout_ms = _remaining_ms(deadline)
        connect_seconds = max(2, math.floor(timeout_ms / 1000))
        collector_engine = create_engine(parsed.set(drivername="postgresql+psycopg"),
            poolclass=NullPool, hide_parameters=True,
            connect_args={"connect_timeout": connect_seconds,
                          "options": f"-c statement_timeout={timeout_ms} -c lock_timeout={timeout_ms} "
                                     "-c default_transaction_read_only=on"})
        with collector_engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                for name, sql in (("connections", CONNECTION_SQL), ("jobs", JOBS_SQL),
                                  ("ai", AI_SQL), ("uploads", UPLOADS_SQL), ("usage", USAGE_SQL)):
                    result[name] = _group(connection, name, sql, parameters, deadline)
            finally:
                transaction.rollback()
    except (SQLAlchemyError, OSError, ValueError, ImportError) as error:
        # Preserve successful earlier groups if a later connection/rollback
        # fails; all still-uncollected fields already carry explicit unknowns.
        if isinstance(error, MonitoringError) and error.code == "database_timeout":
            for name in GROUP_FIELDS:
                if result[name].get("code") == "database_unavailable":
                    result[name] = _unavailable(name, "database_timeout")
    finally:
        if collector_engine is not None:
            collector_engine.dispose()
    result["connections"]["scope"] = "cluster_client_backends_including_collector"
    result["jobs"]["oldest_ready_scope"] = "since_later_of_created_at_and_run_after"
    result["uploads"].update(http_failure_rate=None, http_failure_rate_status="unavailable",
        scope="upload_sessions_created_window",
        processing_failure_scope="image_process_and_pdf_import_jobs_created_window",
        pdf_rejection_scope="pdf_imports_created_window")
    result["usage"].update(cost=None, cost_status="unavailable", scope="ai_runs_finished_window")
    statuses = [result[name]["status"] for name in GROUP_FIELDS]
    result.update(status="unavailable" if all(value == "unavailable" for value in statuses)
                  else "ok" if all(value == "ok" for value in statuses) else "partial",
                  window_minutes=window_minutes, window_start=parameters["since"].isoformat(),
                  window_end=now.isoformat())
    return result
