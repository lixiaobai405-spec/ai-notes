"""PDF object cleanup includes a PUT whose successful write lost its response."""
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.modules.files import service as files
from app.modules.files.models import File, FileVariant, NotePage
from app.modules.files.oss_storage import StorageUnavailable
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import Job
from app.worker import tick
from test_pdf_imports import ready_to_run


@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))


@pytest.mark.parametrize("kind", ["original", "reading", "thumbnail"])
@pytest.mark.parametrize("stored_before_error", [False, True])
def test_pdf_put_failure_registers_attempt_before_request_and_retry_has_only_published_objects(
        client, monkeypatch, kind, stored_before_error):
    item, original = ready_to_run(client)
    adapter = files.storage()
    original_put = adapter.put
    attempted = []

    def failing_put(key, data):
        if key.endswith(f"-{kind}.jpg"):
            attempted.append(key)
            if stored_before_error:
                original_put(key, data)
            raise StorageUnavailable()
        original_put(key, data)

    monkeypatch.setattr(files, "storage", lambda: adapter)
    monkeypatch.setattr(adapter, "put", failing_put)
    assert tick()
    assert len(attempted) == 1
    assert list(Path(settings().storage_root).rglob("*.jpg")) == []
    with SessionLocal.begin() as db:
        source = db.get(File, UUID(item["upload"]["file"]["id"]))
        assert adapter.read(source.object_key) == original
        assert list(db.scalars(select(NotePage))) == []
        assert list(db.scalars(select(FileVariant))) == []
        job = db.scalar(select(Job).where(Job.kind == "pdf.import"))
        assert job.status == "queued" and job.error_code == "handler_failed"
        job.run_after = utcnow()
    monkeypatch.setattr(adapter, "put", original_put)
    assert tick()
    with SessionLocal() as db:
        pages = list(db.scalars(select(NotePage)))
        variants = list(db.scalars(select(FileVariant)))
        assert len(pages) == 3 and len(variants) == 6
        referenced = {db.get(File, page.file_id).object_key for page in pages} | {variant.object_key for variant in variants}
        assert len(referenced) == 9
        assert all(adapter.read(key).startswith(b"\xff\xd8") for key in referenced)
    assert len(list(Path(settings().storage_root).rglob("*.jpg"))) == 9
