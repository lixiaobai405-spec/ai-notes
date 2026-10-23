"""Whole-site backups use real pg_dump/pg_restore against disposable databases.

Only the explicitly configured test PostgreSQL container is used. Every restore
target is created by this module and guarded again before it is dropped.
"""

from datetime import timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import sys
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.modules.ai.models import AiConversation, AiCredential, AiRun, AiStep
from app.modules.files.models import File, FileTicket, FileVariant, NotePage, UploadSession
from app.modules.notes.models import Note
from app.shared.db import Base, SessionLocal, engine, utcnow
from app.shared.models import (Audit, AuthSession, Invite, Job, Notification, Outbox,
                               RateBucket, ResetToken, User)
from app.shared.security import hasher, verify_password
from test_exports import account as export_account


ROOT = Path(__file__).resolve().parents[2]
NORMALIZED_TABLES = {"sessions", "file_tickets", "password_resets", "ai_credentials",
                     "jobs", "ai_runs", "ai_steps"}


@pytest.fixture(scope="module")
def site_tool():
    directory = str(ROOT / "scripts")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module("site_backup")


@pytest.fixture(scope="module")
def site_support(site_tool):
    return importlib.import_module("site_backup_support")


@pytest.fixture(scope="module")
def postgres_container():
    value = os.environ.get("SITE_BACKUP_TEST_CONTAINER")
    if not value:
        pytest.skip("Set SITE_BACKUP_TEST_CONTAINER for real PostgreSQL backup integration")
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value)
    assert engine.url.database == "notes_test"
    return value


def _owned_name(value):
    assert re.fullmatch(r"notes_restore_site_test_[0-9a-f]{32}", value)
    return value


@pytest.fixture
def site_target(postgres_container, tmp_path):
    name = _owned_name("notes_restore_site_test_" + uuid.uuid4().hex)
    url = engine.url.set(database=name)
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    target_engine = None
    # This is always the isolated pytest cluster, never notes_dev or a server.
    assert admin.url.database == "notes_test"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{_owned_name(name)}"'))
    try:
        target_engine = create_engine(url)
        root = tmp_path / "restored-objects"
        root.mkdir()
        yield SimpleNamespace(name=name, url=url.render_as_string(hide_password=False),
                              engine=target_engine, sessions=sessionmaker(bind=target_engine),
                              storage_root=root, container=postgres_container)
    finally:
        if target_engine is not None:
            assert target_engine.url.database == _owned_name(name)
            target_engine.dispose()
        assert admin.url.database == "notes_test"
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{_owned_name(name)}" WITH (FORCE)'))
        admin.dispose()


def _rows(database_engine):
    with database_engine.connect() as connection:
        return {table.name: [dict(row) for row in connection.execute(
            select(table).order_by(*table.primary_key.columns)).mappings()]
            for table in Base.metadata.sorted_tables}


@pytest.fixture
def site_account(export_account):
    """Three private accounts plus runtime state that must not resume on restore."""
    fixture = export_account
    contents = dict(fixture.contents)
    with SessionLocal.begin() as db:
        admin = db.get(User, fixture.owner)
        other = db.get(User, fixture.outsider)
        other.password_hash = hasher.hash("site-other-only-password")
        third = User(username="third-member", display_name="第三位普通用户",
                     password_hash=hasher.hash("site-third-only-password"))
        db.add(third)
        db.flush()
        third_note = Note(owner_id=third.id, title="第三人的私密笔记", plain_text="跨账号恢复内容")
        other_image = Note(owner_id=other.id, title="第二人的图片资料", kind="image")
        db.add_all([third_note, other_image])
        db.flush()
        payload = b"second-account-original-image"
        other_file = File(owner_id=other.id, object_key=f"{other.id}/{uuid.uuid4()}/original",
                          filename="other.png", mime="image/png", status="ready",
                          expected_bytes=len(payload), bytes=len(payload),
                          sha256=hashlib.sha256(payload).hexdigest())
        db.add(other_file)
        db.flush()
        fixture.store.put(other_file.object_key, payload)
        contents[other_file.object_key] = payload
        db.add(NotePage(owner_id=other.id, note_id=other_image.id, file_id=other_file.id, position=0))
        for kind in ("reading", "thumbnail"):
            data = ("second-account-" + kind).encode()
            key = f"{other.id}/{other_file.id}/{kind}"
            db.add(FileVariant(file_id=other_file.id, kind=kind, object_key=key, bytes=len(data)))
            fixture.store.put(key, data)
            contents[key] = data

        # This old object is only referenced by the immutable AI source snapshot.
        frozen = b"historical-reading-bytes-unchanged"
        frozen_key = f"{admin.id}/{fixture.files[1]}/historical-reading"
        run = db.get(AiRun, fixture.runs[0])
        run.source = {**run.source, "pages": [{**run.source["pages"][0],
                                              "object_key": frozen_key, "bytes": len(frozen)}]}
        fixture.store.put(frozen_key, frozen)
        contents[frozen_key] = frozen

        parent_note = db.get(NotePage, fixture.pages[1]).note_id
        pending = File(owner_id=admin.id, object_key=f"{admin.id}/{uuid.uuid4()}/pending",
                       filename="pending.png", mime="image/png", expected_bytes=20)
        rejected = File(owner_id=admin.id, object_key=f"{admin.id}/{uuid.uuid4()}/rejected",
                        filename="rejected.png", mime="image/png", expected_bytes=20,
                        status="rejected", error_code="image_invalid")
        db.add_all([pending, rejected])
        db.flush()
        upload = UploadSession(owner_id=admin.id, file_id=pending.id, note_id=parent_note,
                               request_key=uuid.uuid4().hex, status="pending",
                               expires_at=utcnow() + timedelta(hours=1))
        db.add(upload)
        db.add(AuthSession(user_id=third.id, token_hash=uuid.uuid4().hex, csrf_hash="third-csrf",
                           expires_at=utcnow() + timedelta(hours=1)))
        db.add(FileTicket(owner_id=admin.id, file_id=fixture.files[0], token_hash=uuid.uuid4().hex,
                          expires_at=utcnow() + timedelta(hours=1)))
        db.add(ResetToken(user_id=other.id, token_hash=uuid.uuid4().hex,
                          expires_at=utcnow() + timedelta(hours=1)))
        db.add(AiCredential(owner_id=admin.id, provider="historical-provider",
                            ciphertext=b"x" * 48, key_suffix="test"))
        db.add(Invite(creator_id=admin.id, code_hash=uuid.uuid4().hex,
                      label="恢复后保留的邀请", max_uses=3, used_count=1,
                      expires_at=utcnow() + timedelta(days=1)))
        db.add(Audit(actor_id=admin.id, action="site.backup.fixture", request_id="synthetic",
                     details={"private": "原样保留的审计记录"}))
        db.add(RateBucket(key="site-fixture", count=3, expires_at=utcnow() + timedelta(minutes=1)))
        jobs = []
        for index, (owner, state) in enumerate(((admin, "queued"), (other, "running"), (third, "succeeded"))):
            job = Job(owner_id=owner.id, kind="image.process", status=state,
                      dedupe_key=f"site-fixture-{index}", payload={"synthetic": True},
                      lease_until=utcnow() + timedelta(minutes=1) if state == "running" else None,
                      worker_token="synthetic-worker" if state == "running" else None)
            db.add(job)
            jobs.append(job)
        db.flush()
        event = Outbox(user_id=third.id, job_id=jobs[2].id, kind="job.completed",
                       payload={"private": "应保留的历史事件"}, delivered_at=utcnow())
        db.add(event)
        db.flush()
        db.add(Notification(user_id=third.id, event_id=event.id, message="已完成的历史通知"))
        conversation = AiConversation(owner_id=third.id, request_key=uuid.uuid4(), title="中断恢复演练")
        db.add(conversation)
        db.flush()
        active_runs = []
        for state in ("queued", "running"):
            active = AiRun(owner_id=third.id, conversation_id=conversation.id, request_key=uuid.uuid4(),
                           request_hash=uuid.uuid4().hex, model="synthetic-no-provider",
                           prompt="不能在恢复后自动重发", input_messages=[], answer="已保存的部分回答",
                           status=state, lease_until=utcnow() + timedelta(minutes=1))
            db.add(active)
            active_runs.append(active)
        db.flush()
        step = AiStep(run_id=active_runs[1].id, position=0, kind="answer", pages=[],
                      status="running", answer="付费调用前已落盘的部分回答")
        db.add(step)
        db.flush()
        result = SimpleNamespace(**vars(fixture), third=third.id, third_note=third_note.id,
                                 other_file=other_file.id, frozen_key=frozen_key,
                                 pending=pending.id, rejected=rejected.id, upload=upload.id,
                                 active_runs=[value.id for value in active_runs], step=step.id,
                                 jobs=[value.id for value in jobs], all_contents=contents)
    result.original_rows = _rows(engine)
    return result


def _backup(tool, support, account, container, output, **overrides):
    arguments = {"database_url": engine.url.render_as_string(hide_password=False),
                 "postgres_container": container, "output": output,
                 "storage": support.LocalBackupStorage(account.store.root),
                 "maintenance_confirmed": True}
    arguments.update(overrides)
    return tool.backup_site(**arguments)


@pytest.fixture
def site_bundle(site_tool, site_support, site_account, postgres_container, tmp_path):
    path = tmp_path / "site-backup"
    result = _backup(site_tool, site_support, site_account, postgres_container, path)
    manifest = site_tool.verify_site_backup(path)
    return SimpleNamespace(path=path, result=result, manifest=manifest)


def _restore(tool, path, target, **overrides):
    arguments = {"database_url": target.url, "postgres_container": target.container,
                 "storage_root": target.storage_root, "isolated_confirmed": True}
    arguments.update(overrides)
    return tool.restore_site_backup(path, **arguments)


def _empty_target(target):
    with target.engine.connect() as connection:
        return connection.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")) == 0


def test_real_full_site_roundtrip_preserves_multiuser_rows_frozen_objects_and_revokes_runtime_state(
    site_tool, site_bundle, site_account, site_target,
):
    source = site_account.original_rows
    result = _restore(site_tool, site_bundle.path, site_target)
    after = _rows(site_target.engine)
    assert len(after["users"]) == 3
    for table in source.keys() - NORMALIZED_TABLES:
        assert after[table] == source[table], table
    assert {row["role"] for row in after["users"]} == {"admin", "user"}
    with site_target.sessions() as db:
        assert verify_password(db.get(User, site_account.outsider).password_hash, "site-other-only-password")
        assert verify_password(db.get(User, site_account.third).password_hash, "site-third-only-password")
        assert db.get(Note, site_account.third_note).owner_id == site_account.third
        assert db.get(File, site_account.other_file).owner_id == site_account.outsider
        assert db.get(AiRun, site_account.runs[0]).source["pages"][0]["object_key"] == site_account.frozen_key
        for session in db.scalars(select(AuthSession)):
            assert session.revoked_at is not None
        assert not any(after[name] for name in ("file_tickets", "password_resets", "ai_credentials"))
        for job_id in site_account.jobs[:2]:
            job = db.get(Job, job_id)
            assert (job.status, job.error_code, job.lease_until, job.worker_token) == (
                "failed", "site_restore_interrupted", None, None)
        assert db.get(Job, site_account.jobs[2]).status == "succeeded"
        for run_id in site_account.active_runs:
            run = db.get(AiRun, run_id)
            assert (run.status, run.error_code, run.lease_until) == ("interrupted", "site_restore_interrupted", None)
            assert run.finished_at is not None and run.answer == "已保存的部分回答"
        step = db.get(AiStep, site_account.step)
        assert (step.status, step.error_code) == ("failed", "site_restore_interrupted")
        assert step.answer == "付费调用前已落盘的部分回答"
    expected = site_account.all_contents
    restored = {path.relative_to(site_target.storage_root).as_posix(): path.read_bytes()
                for path in site_target.storage_root.rglob("*") if path.is_file()}
    assert restored == expected
    assert result["objects"] == len(expected)
    assert result["original_fingerprints_verified"] is True
    assert result["missing_optional_objects"] == 3
    assert result["normalization"]["sessions"] == 3
    assert result["normalization"]["jobs"] == 2
    assert result["normalization"]["ai_runs"] == 2
    assert result["normalization"]["ai_steps"] == 1
    for name in ("ai_runs", "ai_steps", "jobs"):
        final_rows = {row["id"]: row for row in after[name]}
        for previous in source[name]:
            if previous["status"] == "succeeded":
                assert final_rows[previous["id"]] == previous
    restored_sessions = {row["id"]: row for row in after["sessions"]}
    for previous in source["sessions"]:
        current = restored_sessions[previous["id"]]
        assert {key: value for key, value in current.items() if key != "revoked_at"} == {
            key: value for key, value in previous.items() if key != "revoked_at"}
    # Restoring an isolated copy must not revoke or normalize the source.
    assert _rows(engine) == source


def test_manifest_records_every_table_exact_object_bytes_and_known_optional_absence(site_bundle, site_account):
    manifest = site_bundle.manifest
    assert manifest["format_version"] == 1
    assert manifest["source_storage"] == "local"
    assert manifest["scope"] == "postgresql_all_public_tables_and_referenced_objects_selected_versions"
    assert manifest["postgres_major"] == 16
    for table, rows in site_account.original_rows.items():
        assert manifest["tables"][table]["count"] == len(rows)
        assert re.fullmatch(r"[0-9a-f]{64}", manifest["tables"][table]["sha256"])
    assert (site_bundle.path / manifest["dump"]["path"]).read_bytes().startswith(b"PGDMP")
    entries = {entry["key"]: entry for entry in manifest["objects"]}
    assert len(entries) == len(manifest["objects"])
    for key, raw in site_account.all_contents.items():
        entry = entries[key]
        assert entry["status"] == "present" and entry["mandatory"] is True
        assert entry["path"] == "objects/" + hashlib.sha256(key.encode()).hexdigest()
        assert entry["size"] == len(raw) and entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert (site_bundle.path / entry["path"]).read_bytes() == raw
    missing = [entry for entry in entries.values() if entry["status"] == "missing"]
    assert len(missing) == 3
    assert all(not entry["mandatory"] and entry["reason"] == "not_found_optional" for entry in missing)
    assert site_account.frozen_key in entries


@pytest.mark.parametrize("damage", ["dump", "object", "extra_file", "path_escape", "duplicate_key"])
def test_corrupt_bundle_is_rejected_before_restore_writes(site_tool, site_bundle, site_target, tmp_path, damage):
    path = tmp_path / "damaged-backup"
    shutil.copytree(site_bundle.path, path)
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    present = next(entry for entry in manifest["objects"] if entry["status"] == "present")
    if damage == "dump":
        dump = path / manifest["dump"]["path"]
        raw = dump.read_bytes()
        dump.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    elif damage == "object":
        (path / present["path"]).write_bytes(b"damaged-private-content")
    elif damage == "extra_file":
        (path / "unexpected.env").write_text("synthetic-private-file", encoding="utf-8")
    elif damage == "path_escape":
        present["path"] = "../outside-object"
    else:
        manifest["objects"].append(dict(present))
    if damage in {"path_escape", "duplicate_key"}:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(site_tool.SiteBackupError) as error:
        _restore(site_tool, path, site_target)
    assert error.value.code == "backup_invalid"
    assert _empty_target(site_target)
    assert list(site_target.storage_root.iterdir()) == []


@pytest.mark.parametrize("kind", ["original", "frozen", "received_upload"])
def test_missing_required_object_fails_without_publishing_backup(
    site_tool, site_support, site_account, postgres_container, tmp_path, kind,
):
    if kind == "original":
        key = next(iter(site_account.contents))
        site_account.store.delete(key)
    elif kind == "frozen":
        site_account.store.delete(site_account.frozen_key)
    else:
        with SessionLocal.begin() as db:
            db.get(UploadSession, site_account.upload).status = "received"
    output = tmp_path / "incomplete-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output)
    assert error.value.code == "backup_object_missing"
    assert not output.exists()


@pytest.mark.parametrize("damage", ["length", "sha256"])
def test_original_integrity_mismatch_fails_backup(
    site_tool, site_support, site_account, postgres_container, tmp_path, damage,
):
    with SessionLocal.begin() as db:
        original = db.get(File, site_account.files[0])
        if damage == "sha256":
            original.sha256 = "0" * 64
        else:
            original.bytes += 1
    output = tmp_path / "mismatch-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output)
    assert error.value.code == "backup_object_mismatch"
    assert not output.exists()


def test_optional_object_storage_errors_are_not_misreported_as_missing(
    site_tool, site_support, site_account, postgres_container, tmp_path, monkeypatch,
):
    storage = site_support.LocalBackupStorage(site_account.store.root)
    actual_capture = storage.capture
    with SessionLocal() as db:
        optional_key = db.get(File, site_account.expired).object_key

    def unavailable(key, destination, max_bytes):
        if key == optional_key:
            raise site_tool.SiteBackupError("site_storage_unavailable")
        return actual_capture(key, destination, max_bytes)

    monkeypatch.setattr(storage, "capture", unavailable)
    output = tmp_path / "unavailable-storage-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output, storage=storage)
    assert error.value.code == "site_storage_unavailable"
    assert not output.exists()


def test_consistent_but_omitted_object_manifest_fails_after_database_reference_validation(
    site_tool, site_bundle, site_target, tmp_path,
):
    path = tmp_path / "omitted-reference-backup"
    shutil.copytree(site_bundle.path, path)
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    removed = next(entry for entry in manifest["objects"] if entry["status"] == "present")
    manifest["objects"].remove(removed)
    (path / removed["path"]).unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # File checksums alone cannot prove that every database reference was copied.
    site_tool.verify_site_backup(path)
    with pytest.raises(site_tool.SiteBackupError) as error:
        _restore(site_tool, path, site_target)
    assert error.value.code == "restore_verification_failed"
    assert not _empty_target(site_target)
    assert (site_target.storage_root / ".site-restore-incomplete").is_file()
    with site_target.sessions() as db:
        assert all(session.revoked_at is None for session in db.scalars(select(AuthSession)))


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_OBJECT_BYTES", "MAX_TOTAL_BYTES", "MAX_DUMP_BYTES", "MAX_MANIFEST_BYTES"])
def test_backup_limits_are_enforced_before_publication(
    site_tool, site_support, site_account, postgres_container, tmp_path, monkeypatch, limit,
):
    monkeypatch.setattr(site_tool, limit, 1)
    output = tmp_path / "limited-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output)
    assert error.value.code == "backup_limit_exceeded"
    assert not output.exists()


def test_backup_requires_explicit_maintenance_and_never_overwrites_destination(
    site_tool, site_support, site_account, postgres_container, tmp_path,
):
    output = tmp_path / "existing-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output, maintenance_confirmed=False)
    assert error.value.code == "backup_maintenance_required" and not output.exists()
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output)
    assert error.value.code == "backup_destination_exists"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_dump_and_manifest_use_the_same_exported_database_snapshot(
    site_tool, site_support, site_account, postgres_container, site_target, tmp_path, monkeypatch,
):
    original_dump = site_tool.DockerPostgres.dump
    previous = next(row["title"] for row in site_account.original_rows["notes"]
                    if row["id"] == site_account.text)
    calls = []

    def dump_after_concurrent_write(postgres, snapshot, destination, max_bytes):
        # Exercise the synchronized database snapshot, independently of the
        # maintenance-window requirement needed to keep object storage stable.
        with SessionLocal.begin() as db:
            db.get(Note, site_account.text).title = "快照导出后提交的新标题"
        calls.append(snapshot)
        return original_dump(postgres, snapshot, destination, max_bytes)

    monkeypatch.setattr(site_tool.DockerPostgres, "dump", dump_after_concurrent_write)
    output = tmp_path / "consistent-snapshot-backup"
    _backup(site_tool, site_support, site_account, postgres_container, output)
    assert len(calls) == 1
    _restore(site_tool, output, site_target)
    with site_target.sessions() as db:
        assert db.get(Note, site_account.text).title == previous
    with SessionLocal() as db:
        assert db.get(Note, site_account.text).title == "快照导出后提交的新标题"


def test_wrong_postgres_container_fails_before_backup_publication(
    site_tool, site_support, site_account, postgres_container, tmp_path,
):
    output = tmp_path / "wrong-container-backup"
    with pytest.raises(site_tool.SiteBackupError) as error:
        _backup(site_tool, site_support, site_account, postgres_container, output,
                postgres_container="site-backup-no-such-container-" + uuid.uuid4().hex)
    assert error.value.code == "site_postgres_failed"
    assert not output.exists()


@pytest.mark.parametrize("kind", ["confirmation", "database", "storage", "wrong_database", "extra_schema"])
def test_restore_requires_confirmed_empty_isolated_targets(site_tool, site_bundle, site_target, kind):
    overrides = {}
    expected = "restore_isolation_required"
    if kind == "confirmation":
        overrides["isolated_confirmed"] = False
    elif kind == "database":
        with site_target.engine.begin() as connection:
            connection.execute(text("CREATE TABLE keep_private_data (value text)"))
            connection.execute(text("INSERT INTO keep_private_data VALUES ('keep')"))
        expected = "restore_target_not_empty"
    elif kind == "storage":
        (site_target.storage_root / "keep.txt").write_text("keep", encoding="utf-8")
        expected = "restore_storage_not_empty"
    elif kind == "extra_schema":
        with site_target.engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA pgextra"))
        expected = "backup_schema_invalid"
    else:
        overrides["database_url"] = engine.url.render_as_string(hide_password=False)
        expected = "restore_target_invalid"
    before = _rows(engine)
    with pytest.raises(site_tool.SiteBackupError) as error:
        _restore(site_tool, site_bundle.path, site_target, **overrides)
    assert error.value.code == expected
    assert _rows(engine) == before
    if kind == "database":
        with site_target.engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM keep_private_data")) == "keep"
    else:
        assert _empty_target(site_target)
    if kind == "storage":
        assert (site_target.storage_root / "keep.txt").read_text(encoding="utf-8") == "keep"
