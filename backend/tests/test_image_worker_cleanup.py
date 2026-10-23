"""Local fault injection for image variant publication and conservative cleanup."""
from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import threading
from uuid import UUID
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.modules.files import service
from app.modules.files.images import process_image
from app.modules.files.models import File, FileVariant
from app.modules.files.oss_storage import StorageUnavailable
from app.modules.jobs import service as jobs
from app.shared.config import settings
from app.shared.db import SessionLocal, engine, utcnow
from app.shared.models import Job, Outbox
from app.worker import tick
from conftest import login
from test_files import note, upload, put, complete


@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))


def pending_image(client):
    login(client)
    target = note(client)
    item, data = upload(client, target)
    assert put(client, item, data).status_code == 200
    assert complete(client, item, 1).status_code == 200
    return UUID(item["file"]["id"]), data


def jpeg_objects():
    return list(Path(settings().storage_root).rglob("*.jpg"))


@pytest.mark.parametrize("kind", ["reading", "thumbnail"])
@pytest.mark.parametrize("stored_before_error", [False, True])
def test_partial_put_failure_discards_attempt_objects_and_retry_has_only_two_variants(
        client, monkeypatch, kind, stored_before_error):
    file_id, original = pending_image(client)
    adapter = service.storage()
    original_put = adapter.put

    def failing_put(key, data):
        if key.endswith(f"-{kind}.jpg"):
            if stored_before_error:
                original_put(key, data)
            raise StorageUnavailable()
        original_put(key, data)

    monkeypatch.setattr(service, "storage", lambda: adapter)
    monkeypatch.setattr(adapter, "put", failing_put)
    assert tick()
    assert jpeg_objects() == []
    with SessionLocal.begin() as db:
        file = db.get(File, file_id)
        assert file.status == "processing" and adapter.read(file.object_key) == original
        assert list(db.scalars(select(FileVariant))) == []
        job = db.scalar(select(Job).where(Job.kind == "image.process"))
        assert job.status == "queued" and job.error_code == "handler_failed"
        assert list(db.scalars(select(Outbox))) == []
        job.run_after = utcnow()
    monkeypatch.setattr(adapter, "put", original_put)
    assert tick()
    with SessionLocal() as db:
        assert db.get(File, file_id).status == "ready"
        variants = list(db.scalars(select(FileVariant).where(FileVariant.file_id == file_id)))
        assert len(variants) == 2
        assert all(adapter.read(variant.object_key).startswith(b"\xff\xd8") for variant in variants)
    assert len(jpeg_objects()) == 2


def test_stale_lease_discards_only_old_tokens_objects(client):
    file_id, _ = pending_image(client)
    old = jobs.claim()
    with SessionLocal.begin() as db:
        db.get(Job, old["id"]).lease_until = utcnow() - timedelta(seconds=1)
    new = jobs.claim()
    assert new["token"] != old["token"]
    assert service.process_job(new)
    with SessionLocal() as db:
        current = {v.object_key for v in db.scalars(select(FileVariant).where(FileVariant.file_id == file_id))}
    assert not service.process_job(old)
    assert len(jpeg_objects()) == 2
    with SessionLocal() as db:
        assert {v.object_key for v in db.scalars(select(FileVariant))} == current
        assert db.get(File, file_id).status == "ready"
    assert all(service.storage().read(key).startswith(b"\xff\xd8") for key in current)


def test_publication_rollback_discards_all_unpublished_variants(client, monkeypatch):
    file_id, _ = pending_image(client)
    factory = service.SessionLocal

    @contextmanager
    def rollback_publication():
        with factory.begin() as db:
            yield db
            raise RuntimeError("publication_failed_before_commit")

    sessions = Mock(wraps=factory)
    sessions.begin.side_effect = rollback_publication
    monkeypatch.setattr(service, "SessionLocal", sessions)
    assert tick()
    assert jpeg_objects() == []
    with SessionLocal() as db:
        assert db.get(File, file_id).status == "processing"
        assert list(db.scalars(select(FileVariant))) == []
        assert list(db.scalars(select(Outbox))) == []
        assert db.scalar(select(Job)).status == "queued"


def test_lost_commit_acknowledgement_preserves_published_variants(client, monkeypatch):
    file_id, _ = pending_image(client)
    claimed = jobs.claim()
    factory = service.SessionLocal

    @contextmanager
    def commit_then_disconnect():
        with factory.begin() as db:
            yield db
        raise RuntimeError("publication_commit_acknowledgement_lost")

    sessions = Mock(wraps=factory)
    sessions.begin.side_effect = commit_then_disconnect
    monkeypatch.setattr(service, "SessionLocal", sessions)
    with pytest.raises(RuntimeError, match="publication_commit_acknowledgement_lost"):
        service.process_job(claimed)
    assert not jobs.fail(claimed["id"], claimed["token"])
    with SessionLocal() as db:
        assert db.get(File, file_id).status == "ready"
        assert db.get(Job, claimed["id"]).status == "succeeded"
        variants = list(db.scalars(select(FileVariant)))
        assert len(variants) == 2 and len(list(db.scalars(select(Outbox)))) == 1
    assert len(jpeg_objects()) == 2
    assert all(service.storage().read(v.object_key).startswith(b"\xff\xd8") for v in variants)


def test_unknown_database_state_retains_objects_and_original_failure(client, monkeypatch):
    file_id, _ = pending_image(client)
    claimed = jobs.claim()
    adapter = service.storage()
    original_put = adapter.put
    original_error = StorageUnavailable()

    def failing_thumbnail(key, data):
        if key.endswith("-thumbnail.jpg"):
            raise original_error
        original_put(key, data)

    @contextmanager
    def database_unavailable():
        raise OperationalError("SELECT variant references", {}, RuntimeError("database unavailable"))
        yield  # pragma: no cover

    # First session reads File normally; cleanup's independent check is unavailable.
    sessions = Mock(side_effect=[SessionLocal(), database_unavailable()])
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service, "storage", lambda: adapter)
    monkeypatch.setattr(adapter, "put", failing_thumbnail)
    with pytest.raises(StorageUnavailable) as error:
        service.process_job(claimed)
    assert error.value is original_error
    assert len(jpeg_objects()) == 1
    with SessionLocal() as db:
        assert db.get(File, file_id).status == "processing"
        assert db.get(Job, claimed["id"]).status == "running"
        assert list(db.scalars(select(FileVariant))) == []


def test_cleanup_failure_never_masks_processing_error_or_publishes_success(client, monkeypatch):
    file_id, _ = pending_image(client)
    claimed = jobs.claim()
    adapter = service.storage()
    original_put = adapter.put
    original_error = RuntimeError("original_thumbnail_write_error")

    def failing_thumbnail(key, data):
        if key.endswith("-thumbnail.jpg"):
            raise original_error
        original_put(key, data)

    def failing_cleanup(_):
        raise StorageUnavailable()

    monkeypatch.setattr(service, "storage", lambda: adapter)
    monkeypatch.setattr(adapter, "put", failing_thumbnail)
    monkeypatch.setattr(adapter, "delete", failing_cleanup)
    with pytest.raises(RuntimeError) as error:
        service.process_job(claimed)
    assert error.value is original_error
    assert jobs.fail(claimed["id"], claimed["token"])
    with SessionLocal() as db:
        assert db.get(File, file_id).status == "processing"
        assert db.get(Job, claimed["id"]).status == "queued"
        assert list(db.scalars(select(FileVariant))) == []
        assert list(db.scalars(select(Outbox))) == []
    assert len(jpeg_objects()) == 1


def test_existing_variant_conflicts_keep_old_objects_and_discard_new_token_keys(client):
    file_id, original = pending_image(client)
    processed = process_image(original, "image/png", settings().upload_max_pixels)
    adapter = service.storage()
    old = {}
    with SessionLocal.begin() as db:
        prefix = db.get(File, file_id).object_key.rsplit("/", 1)[0]
        for kind, data in (("reading", processed.reading), ("thumbnail", processed.thumbnail)):
            key = f"{prefix}/historical-{kind}.jpg"
            adapter.put(key, data)
            db.add(FileVariant(file_id=file_id, kind=kind, object_key=key, bytes=len(data)))
            old[key] = data
    assert len(jpeg_objects()) == 2
    assert service.process_job(jobs.claim())
    with SessionLocal() as db:
        assert {v.object_key for v in db.scalars(select(FileVariant))} == set(old)
        assert db.get(File, file_id).status == "ready"
        assert db.scalar(select(Job)).status == "succeeded"
    assert len(jpeg_objects()) == 2
    assert all(adapter.read(key) == data for key, data in old.items())


@pytest.mark.skipif(os.getenv("PGLITE_TEST") == "1", reason="Requires independent PostgreSQL connections")
@pytest.mark.parametrize("decision", ["commit", "rollback", "timeout"])
def test_cleanup_waits_for_inflight_publication_before_deciding_references(client, decision):
    file_id, original = pending_image(client)
    data = process_image(original, "image/png", settings().upload_max_pixels).reading
    adapter = service.storage()
    lock_requested, finished = threading.Event(), threading.Event()
    thread = None

    def observe_lock(_connection, _cursor, statement, _parameters, _context, _executemany):
        if threading.current_thread().name == "variant-cleanup-audit" and "FOR UPDATE" in statement:
            lock_requested.set()

    def cleanup(key):
        try:
            service._discard_unpublished_variants(file_id, [("reading", key, len(data))])
        finally:
            finished.set()

    event.listen(engine, "before_cursor_execute", observe_lock)
    try:
        with SessionLocal() as publisher:
            file = publisher.scalar(select(File).where(File.id == file_id).with_for_update())
            key = file.object_key.rsplit("/", 1)[0] + "/uncertain-commit-reading.jpg"
            adapter.put(key, data)
            publisher.add(FileVariant(file_id=file_id, kind="reading", object_key=key, bytes=len(data)))
            publisher.flush()  # Reference exists only inside the publisher transaction.
            thread = threading.Thread(target=cleanup, args=(key,), name="variant-cleanup-audit", daemon=True)
            thread.start()
            assert lock_requested.wait(5), "cleanup never requested the publication synchronization lock"
            assert not finished.wait(0.2), "cleanup decided before the publication transaction completed"
            assert adapter.read(key) == data
            if decision == "commit":
                publisher.commit()
            elif decision == "rollback":
                publisher.rollback()
            assert finished.wait(5), "cleanup did not finish after publication transaction resolution"
        if decision != "rollback":
            assert adapter.read(key) == data
        else:
            with pytest.raises(FileNotFoundError):
                adapter.read(key)
    finally:
        event.remove(engine, "before_cursor_execute", observe_lock)
        if thread is not None:
            thread.join(timeout=5)
