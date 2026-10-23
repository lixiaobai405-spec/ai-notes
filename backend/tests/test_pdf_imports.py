from datetime import timedelta
import io
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.main import app
from app.modules.files import pdf_service, service as files
from app.modules.files.models import File, FileVariant, NotePage, PdfImport, UploadSession
from app.modules.files.pdf_render import PdfRenderFailure, render_pdf
from app.modules.files.oss_storage import StorageUnavailable
from app.modules.jobs import service as jobs
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import Job, User
from app.worker import tick
from conftest import invite, login, mutate, register


@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))


def pdf():
    images = [Image.new("RGB", (200, 300), color) for color in ("red", "green", "blue")]
    output = io.BytesIO()
    images[0].save(output, format="PDF", save_all=True, append_images=images[1:])
    return output.getvalue()


def create(client, data=None, key="pdf-import-test-001", **changes):
    data = pdf() if data is None else data
    response = mutate(client, "POST", "/pdf-imports", {"filename": "学习笔记.pdf", "size": len(data), "request_key": key, **changes})
    assert response.status_code == 201, response.text
    return response.json(), data


def send(client, item, data):
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    return client.put(item["upload"]["upload_url"], content=data,
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf, "Content-Type": "application/pdf"})


def begin(client, item):
    return mutate(client, "POST", f"/pdf-imports/{item['id']}/start", {})


def get(client, item):
    return client.get(f"/api/v1/pdf-imports/{item['id']}")


def ready_to_run(client, data=None):
    login(client)
    item, data = create(client, data)
    assert send(client, item, data).status_code == 200
    assert begin(client, item).status_code == 200
    return item, data


def test_pdf_import_keeps_original_creates_ordered_readable_pages_and_is_idempotent(client):
    login(client)
    item, original = create(client)
    assert create(client, original)[0]["id"] == item["id"]
    assert begin(client, item).status_code == 409
    assert send(client, item, original).status_code == 200
    assert begin(client, item).json()["status"] == "queued"
    assert begin(client, item).json()["status"] == "queued"
    assert send(client, item, original).status_code == 409
    assert tick()
    result = get(client, item).json()
    assert result["status"] == "ready" and result["page_count"] == result["completed_pages"] == 3
    pages = client.get(f"/api/v1/notes/{item['note_id']}/pages").json()
    assert [page["position"] for page in pages] == [0, 1, 2]
    for index, page in enumerate(pages):
        ticket = mutate(client, "POST", f"/files/{page['file']['id']}/ticket", {})
        content = client.get(ticket.json()["url"])
        image = Image.open(io.BytesIO(content.content))
        channel = max(range(3), key=lambda c: image.getpixel((100, 100))[c])
        assert channel == index and page["file"]["status"] == "ready"
    ticket = mutate(client, "POST", f"/files/{item['upload']['file']['id']}/ticket?kind=original", {})
    response = client.get(ticket.json()["url"])
    assert response.content == original
    assert response.headers["content-disposition"].startswith("attachment")
    assert mutate(client, "POST", f"/files/{item['upload']['file']['id']}/ticket", {}).status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Note)) == 1
        assert db.scalar(select(func.count()).select_from(NotePage)) == 3
        assert db.scalar(select(func.count()).select_from(FileVariant)) == 6
    assert begin(client, item).json()["completed_pages"] == 3


@pytest.mark.parametrize("kind", ["corrupt", "encrypted", "page_limit", "pixels"])
def test_rejected_pdfs_give_clear_errors_and_keep_source(client, monkeypatch, kind):
    data = {"corrupt": b"%PDF-this-is-not-a-document", "encrypted": (Path(__file__).parent / "fixtures/password-required.pdf").read_bytes()}.get(kind)
    item, original = ready_to_run(client, data)
    if kind == "page_limit":
        monkeypatch.setattr(settings(), "pdf_max_pages", 2)
    if kind == "pixels":
        monkeypatch.setattr(settings(), "upload_max_pixels", 10)
    assert tick()
    result = get(client, item).json()
    assert result["status"] == "rejected"
    assert result["error_code"] == {"corrupt": "pdf_invalid", "encrypted": "pdf_password_required", "page_limit": "pdf_page_limit", "pixels": "pdf_page_pixels"}[kind]
    assert result["completed_pages"] == 0
    ticket = mutate(client, "POST", f"/files/{item['upload']['file']['id']}/ticket?kind=original", {})
    assert client.get(ticket.json()["url"]).content == original


def test_size_filename_quota_foreign_notebook_and_dedupe_validation(client, monkeypatch):
    login(client)
    body = {"filename": "book.pdf", "size": 10, "request_key": "pdf-validation-001"}
    assert mutate(client, "POST", "/pdf-imports", {**body, "filename": "bad.png"}).status_code == 422
    assert mutate(client, "POST", "/pdf-imports", {**body, "filename": "bad\u0000.pdf"}).status_code == 422
    assert mutate(client, "POST", "/pdf-imports", {**body, "notebook_id": str(uuid4())}).status_code == 404
    monkeypatch.setattr(settings(), "pdf_max_bytes", 9)
    assert mutate(client, "POST", "/pdf-imports", body).status_code == 413
    monkeypatch.setattr(settings(), "pdf_max_bytes", 100)
    monkeypatch.setattr(settings(), "storage_quota_bytes", 9)
    assert mutate(client, "POST", "/pdf-imports", body).status_code == 413
    monkeypatch.setattr(settings(), "storage_quota_bytes", 1000)
    response = mutate(client, "POST", "/pdf-imports", body)
    assert response.status_code == 201
    assert mutate(client, "POST", "/pdf-imports", {**body, "size": 11}).status_code == 409


def test_original_and_import_are_private_even_from_admin_and_after_trashing(client):
    code = invite(client)["code"]
    with TestClient(app) as member:
        assert register(member, code).status_code == 201
        item, data = create(member)
        assert send(member, item, data).status_code == 200
        assert begin(member, item).status_code == 200
        assert tick()
        file_id = item["upload"]["file"]["id"]
        ticket = mutate(member, "POST", f"/files/{file_id}/ticket?kind=original", {}).json()
        for method, path in [("GET", f"/pdf-imports/{item['id']}"), ("POST", f"/pdf-imports/{item['id']}/retry"),
                             ("GET", f"/notes/{item['note_id']}/pdf-imports"), ("POST", f"/files/{file_id}/ticket?kind=original")]:
            assert mutate(client, method, path, {}).status_code == 404
        assert client.get(ticket["url"]).status_code == 404
        note = member.get(f"/api/v1/notes/{item['note_id']}").json()
        assert mutate(member, "DELETE", f"/notes/{note['id']}", {"revision": note["revision"]}).status_code == 204
        assert member.get(ticket["url"]).status_code == 404
        assert get(member, item).status_code == 404


def test_expired_upload_can_resume_without_new_note_and_confirmed_pdf_is_immutable(client):
    login(client)
    item, data = create(client)
    assert send(client, item, data).status_code == 200
    with SessionLocal.begin() as db:
        db.get(UploadSession, UUID(item["upload"]["id"])).expires_at = utcnow() - timedelta(seconds=1)
    files.maintenance()
    assert get(client, item).json()["status"] == "expired"
    assert mutate(client, "POST", f"/pdf-imports/{item['id']}/retry", {}).status_code == 200
    assert send(client, item, data).status_code == 200
    assert send(client, item, b"x" * len(data)).status_code == 409
    assert begin(client, item).status_code == 200
    assert mutate(client, "POST", f"/uploads/{item['upload']['id']}/complete", {"revision": 1}).status_code == 422


def test_image_mutations_wait_for_pdf_conversion(client):
    item, _ = ready_to_run(client)
    assert mutate(client, "POST", f"/notes/{item['note_id']}/uploads", {
        "filename": "a.png", "mime": "image/png", "size": 10, "request_key": "other-image-test"}).status_code == 409
    assert mutate(client, "PATCH", f"/notes/{item['note_id']}/pages/order", {"revision": 1, "page_ids": [str(uuid4())]}).status_code == 409


def test_partial_storage_failure_retries_from_saved_page_without_duplicates(client, monkeypatch):
    item, _ = ready_to_run(client)
    adapter = files.storage()
    original_put = adapter.put
    calls = 0

    def failing_put(key, data):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise StorageUnavailable()
        return original_put(key, data)

    monkeypatch.setattr(files, "storage", lambda: adapter)
    monkeypatch.setattr(adapter, "put", failing_put)
    assert tick()
    assert get(client, item).json()["completed_pages"] == 1
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == "pdf.import"))
        job.status = "failed"
    assert get(client, item).json()["status"] == "failed"
    assert mutate(client, "POST", f"/pdf-imports/{item['id']}/retry", {}).status_code == 200
    monkeypatch.setattr(adapter, "put", original_put)
    assert tick()
    assert get(client, item).json()["status"] == "ready"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(NotePage)) == 3
        assert db.scalar(select(func.count()).select_from(FileVariant)) == 6
    assert len(list(Path(settings().storage_root).rglob('*.jpg'))) == 9


def test_stale_worker_cannot_publish_or_leave_its_page_objects(client, monkeypatch):
    item, _ = ready_to_run(client)
    claimed = jobs.claim()
    data = io.BytesIO()
    Image.new("RGB", (10, 20), "white").save(data, format="JPEG")

    def superseded(*args, **kwargs):
        yield {"page_count": 3}
        with SessionLocal.begin() as db:
            db.get(Job, claimed["id"]).worker_token = str(uuid4())
        yield {"index": 0, "data": data.getvalue()}

    monkeypatch.setattr(pdf_service, "render_pdf", superseded)
    assert pdf_service.process_job(claimed) is False
    assert get(client, item).json()["completed_pages"] == 0
    assert not list(Path(settings().storage_root).rglob('*.jpg'))


@pytest.mark.parametrize("change", ["deleted", "disabled"])
def test_conversion_stops_if_owner_or_note_becomes_unavailable(client, monkeypatch, change):
    item, _ = ready_to_run(client)
    claimed = jobs.claim()

    def changed(*args, **kwargs):
        with SessionLocal.begin() as db:
            if change == "deleted":
                db.get(Note, UUID(item["note_id"])).deleted_at = utcnow()
            else:
                owner = db.scalar(select(User).where(User.username == "admin"))
                owner.status = "disabled"
        yield {"page_count": 3}

    monkeypatch.setattr(pdf_service, "render_pdf", changed)
    assert pdf_service.process_job(claimed) is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(NotePage)) == 0
        job = db.get(Job, claimed["id"])
        assert job.status == "failed" and job.error_code == "pdf_note_unavailable"


def test_render_timeout_is_retryable_and_output_quota_is_enforced(client, monkeypatch):
    item, original = ready_to_run(client)

    def timeout(*args, **kwargs):
        raise PdfRenderFailure("pdf_render_timeout")
        yield

    with monkeypatch.context() as scoped:
        scoped.setattr(pdf_service, "render_pdf", timeout)
        assert tick()
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == "pdf.import"))
        assert job.error_code == "pdf_render_timeout"
        job.run_after = utcnow()
    monkeypatch.setattr(settings(), "storage_quota_bytes", len(original) + 1)
    assert tick()
    result = get(client, item).json()
    assert result["status"] == "rejected" and result["error_code"] == "storage_quota"
    assert result["completed_pages"] == 0
    assert not list(Path(settings().storage_root).rglob('*.jpg'))


def test_renderer_deadline_stops_the_child_process():
    with pytest.raises(PdfRenderFailure, match="pdf_render_timeout"):
        list(render_pdf(pdf(), 0, max_pages=200, timeout=0.000001,
                        max_bytes=20 * 1024 * 1024, max_pixels=40_000_000))


def test_lost_commit_acknowledgement_does_not_delete_published_page_objects(client, monkeypatch):
    item, _ = ready_to_run(client)
    original_snapshot = pdf_service.notes.save_snapshot
    lost = False

    def commit_then_disconnect(db, note):
        nonlocal lost
        original_snapshot(db, note)
        if not lost:
            lost = True
            db.commit()
            raise RuntimeError("simulated_commit_acknowledgement_loss")

    with monkeypatch.context() as scoped:
        scoped.setattr(pdf_service.notes, "save_snapshot", commit_then_disconnect)
        assert tick()
    assert get(client, item).json()["completed_pages"] == 1
    assert len(list(Path(settings().storage_root).rglob('*.jpg'))) == 3
    with SessionLocal.begin() as db:
        db.scalar(select(Job).where(Job.kind == "pdf.import")).run_after = utcnow()
    assert tick()
    assert get(client, item).json()["status"] == "ready"
    assert len(list(Path(settings().storage_root).rglob('*.jpg'))) == 9
