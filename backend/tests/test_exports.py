import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import uuid
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.modules.ai.models import AiConversation, AiRun, AiSavedNote, AiStep
from app.modules.ai.source_context import public_source
from app.modules.exports import service
from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes.models import Folder, Note, Notebook, NoteTag, NoteVersion, Tag
from app.shared.config import settings
from app.shared.db import SessionLocal, engine, utcnow
from app.shared.errors import AppError
from app.shared.models import AuthSession, Job, User
from conftest import login, mutate
from test_files import complete, note, put, upload


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "objects"))
    store = service.files.storage()
    contents = {}
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        outsider = User(username="other-owner", display_name="不应出现在归档中", password_hash="secret-password-hash")
        db.add(outsider)
        db.flush()
        sessions = []
        for user in (owner, outsider):
            session = AuthSession(user_id=user.id, token_hash=uuid.uuid4().hex, csrf_hash="secret-csrf",
                                  expires_at=utcnow() + timedelta(hours=1))
            db.add(session)
            sessions.append(session)
        folder = Folder(owner_id=owner.id, name="资料")
        db.add(folder)
        db.flush()
        notebook = Notebook(owner_id=owner.id, folder_id=folder.id, name="学习")
        tag = Tag(owner_id=owner.id, name="复习")
        db.add_all([notebook, tag])
        db.flush()
        text = Note(owner_id=owner.id, title="保存的 PDF 回答", notebook_id=notebook.id,
                    body_json={"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "答案 $x^2$"}]}]},
                    plain_text="答案 $x^2$", revision=2)
        trash = Note(owner_id=owner.id, title="回收站内的回答", deleted_at=utcnow())
        pdf_note = Note(owner_id=owner.id, title="PDF 逐页资料", kind="image", revision=3)
        image_note = Note(owner_id=owner.id, title="图片选区资料", kind="image", revision=3)
        foreign = Note(owner_id=outsider.id, title="绝不导出其他人的笔记")
        db.add_all([text, trash, pdf_note, image_note, foreign])
        db.flush()
        db.add(NoteTag(owner_id=owner.id, note_id=text.id, tag_id=tag.id))
        db.add(NoteVersion(owner_id=owner.id, note_id=text.id, revision=1,
            snapshot={"title": "历史原文", "body_json": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "原始公式 α"}]}]},
                      "tag_ids": [str(tag.id)], "notebook_id": str(notebook.id)}))
        file_rows, variants, pages = [], [], []
        for index, payload in enumerate((b"%PDF-synthetic-original", b"page-original-one", b"image-original-two")):
            file_id = uuid.uuid4()
            row = File(id=file_id, owner_id=owner.id, object_key=f"{owner.id}/{file_id}/private-original",
                       filename="../资料.pdf" if index == 0 else "手写.png", mime="application/pdf" if index == 0 else "image/png",
                       expected_bytes=len(payload), bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), status="ready")
            db.add(row)
            db.flush()
            store.put(row.object_key, payload)
            contents[row.object_key] = payload
            file_rows.append(row)
            if index:
                for kind in ("reading", "thumbnail"):
                    value = f"synthetic-{kind}-{index}".encode()
                    variant = FileVariant(file_id=row.id, kind=kind, object_key=f"{owner.id}/{row.id}/private-{kind}", bytes=len(value))
                    db.add(variant)
                    variants.append(variant)
                    store.put(variant.object_key, value)
                    contents[variant.object_key] = value
                page = NotePage(owner_id=owner.id, note_id=pdf_note.id if index == 1 else image_note.id,
                                file_id=row.id, position=0, pdf_page_number=1 if index == 1 else None)
                db.add(page)
                pages.append(page)
        expired = File(owner_id=owner.id, object_key=f"{owner.id}/{uuid.uuid4()}/expired", filename="过期.png",
                       mime="image/png", expected_bytes=10, status="expired")
        db.add(expired)
        db.add(PdfImport(owner_id=owner.id, note_id=pdf_note.id, source_file_id=file_rows[0].id,
                         request_hash="private-pdf-request-hash", status="ready", page_count=1, completed_pages=1))
        conversation = AiConversation(owner_id=owner.id, title="我的完整对话", request_key=uuid.uuid4())
        db.add(conversation)
        db.flush()
        sources, runs = [], []
        for index, (parent, page, dest) in enumerate(zip((pdf_note, image_note), pages, (text, trash), strict=True)):
            variant = variants[index * 2]
            source = {"kind": "pdf" if index == 0 else "image", "note_id": str(parent.id), "title": parent.title,
                      "revision": 1, "mode": "all" if index == 0 else "region", "pages": [{
                          "page_id": str(page.id), "number": 1, "file_id": str(page.file_id),
                          "object_key": variant.object_key, "bytes": variant.bytes}]}
            if index:
                source["pages"][0]["region"] = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
                source["pages"][0]["page_revision"] = 1
            run = AiRun(owner_id=owner.id, conversation_id=conversation.id, request_key=uuid.uuid4(),
                        request_hash="private-ai-request-hash", model="deepseek-flash", prompt="请解释资料", answer="答案 [第1页]",
                        input_messages=[{"role": "system", "content": "private-system-prompt"}], status="succeeded", source=source,
                        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, finished_at=utcnow())
            db.add(run)
            db.flush()
            db.add(AiStep(run_id=run.id, position=0, kind="answer", pages=[1], status="succeeded", answer="完整的分步回答"))
            db.add(AiSavedNote(owner_id=owner.id, run_id=run.id, note_id=dest.id,
                               source=public_source(source).model_dump(mode="json"), cited_page_numbers=[1]))
            sources.append(source)
            runs.append(run)
        db.flush()
        result = SimpleNamespace(owner=owner.id, session=sessions[0].id, outsider=outsider.id,
            outsider_session=sessions[1].id, text=text.id, trash=trash.id, files=[f.id for f in file_rows],
            expired=expired.id, variants=[v.id for v in variants], pages=[p.id for p in pages],
            runs=[r.id for r in runs], keys=list(contents), contents=contents, store=store)
    return result


def read_archive(account):
    with service.create_archive(account.owner, account.session) as stream:
        with zipfile.ZipFile(stream) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist()}
    return json.loads(payloads["manifest.json"]), json.loads(payloads["data.json"])["collections"], payloads


def test_complete_private_export_preserves_versions_pdf_crops_and_checksums(account, tmp_path):
    manifest, collections, payloads = read_archive(account)
    assert manifest["format"] == "ai-notes-export" and manifest["version"] == 1
    assert manifest["owner"]["id"] == str(account.owner)
    assert manifest["resume_processing"] is False
    assert len(collections) == 14
    assert manifest["counts"] == {table: len(rows) for table, rows in collections.items()}
    assert len(collections["notes"]) == 4
    assert next(n for n in collections["notes"] if n["id"] == str(account.trash))["deleted_at"]
    assert collections["note_versions"][0]["snapshot"]["title"] == "历史原文"
    assert collections["note_versions"][0]["snapshot"]["body_json"]["content"][0]["content"][0]["text"] == "原始公式 α"
    assert len(collections["ai_steps"]) == 2
    assert collections["pdf_imports"][0]["source_file_id"] == str(account.files[0])
    assert len(payloads) == 9  # 3 originals, 4 variants, metadata and manifest.
    assert len({p["path"] for p in manifest["payloads"]}) == len(manifest["payloads"])
    assert {p["path"] for p in manifest["payloads"]} == set(payloads) - {"manifest.json"}
    for entry in manifest["payloads"]:
        raw = payloads[entry["path"]]
        assert len(raw) == entry["bytes"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    for table in ("ai_runs", "ai_saved_notes"):
        sources = [row["source"] for row in collections[table]]
        for source in sources:
            page = source["pages"][0]
            assert page["file_id"] in {str(fid) for fid in account.files}
            assert page["reading_path"] in payloads
        crop = next(source for source in sources if source["kind"] == "image")
        assert crop["pages"][0]["region"] == {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
    expired = next(f for f in collections["files"] if f["id"] == str(account.expired))
    assert expired["original_path"] is None
    assert expired["original_unavailable_reason"] == "upload_expired"
    serialized = payloads["data.json"].decode()
    for private in ("object_key", "private-original", "private-reading", "password_hash", "csrf_hash", "token_hash",
                    "input_messages", "private-system-prompt", "request_key", "request_hash", "lease_until", "worker_token",
                    "绝不导出其他人的笔记", str(account.outsider)):
        assert private not in serialized
    _, other, _ = read_archive(SimpleNamespace(owner=account.outsider, session=account.outsider_session))
    assert [n["title"] for n in other["notes"]] == ["绝不导出其他人的笔记"]
    assert not other["files"] and not other["ai_steps"]
    spec = importlib.util.spec_from_file_location("export_verifier", Path(__file__).resolve().parents[2] / "scripts" / "verify_export.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    destination = tmp_path / "verified.zip"
    with service.create_archive(account.owner, account.session) as stream, destination.open("wb") as target:
        shutil.copyfileobj(stream, target)
    assert verifier.verify_archive(destination) == manifest["counts"]


def test_download_requires_auth_csrf_and_returns_private_zip(client, account):
    assert mutate(client, "POST", "/exports/archive", {}).status_code == 401
    login(client)
    assert client.post("/api/v1/exports/archive", json={}, headers={"Origin": "http://testserver"}).status_code == 403
    response = mutate(client, "POST", "/exports/archive", {})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="ai-notes-export-')
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("manifest.json"))["owner"]["id"] == str(account.owner)


@pytest.mark.parametrize("damage", ["original_missing", "variant_missing", "original_bytes", "variant_bytes", "sha256"])
def test_missing_or_corrupt_object_fails_without_a_partial_archive(account, damage):
    with SessionLocal.begin() as db:
        original = db.get(File, account.files[0])
        variant = db.get(FileVariant, account.variants[0])
        if damage == "original_missing":
            account.store.delete(original.object_key)
        elif damage == "variant_missing":
            account.store.delete(variant.object_key)
        elif damage == "original_bytes":
            account.store.put(original.object_key, b"different length")
        elif damage == "variant_bytes":
            account.store.put(variant.object_key, b"different length")
        else:
            original.sha256 = "0" * 64
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 409 and error.value.code == "export_incomplete"


@pytest.mark.parametrize("limit", ["export_max_bytes", "export_max_metadata_bytes", "export_max_rows"])
def test_export_limits_prevent_unbounded_archives(account, monkeypatch, limit):
    monkeypatch.setattr(settings(), limit, 1)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 413


def test_per_object_limit_checks_head_before_read(account, monkeypatch):
    monkeypatch.setattr(settings(), "upload_max_bytes", 1)
    monkeypatch.setattr(settings(), "pdf_max_bytes", 1)
    calls = []
    monkeypatch.setattr(account.store, "read", lambda key: calls.append(key))
    monkeypatch.setattr(service.files, "storage", lambda: account.store)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 413
    assert not calls


@pytest.mark.parametrize("kind", ["upload", "image", "pdf", "ai"])
def test_in_flight_work_blocks_complete_export(account, kind):
    with SessionLocal.begin() as db:
        if kind == "upload":
            db.get(File, account.expired).status = "uploading"
        elif kind == "ai":
            db.get(AiRun, account.runs[0]).status = "running"
        else:
            db.add(Job(owner_id=account.owner, kind="image.process" if kind == "image" else "pdf.import",
                       dedupe_key="active", status="queued"))
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 409 and error.value.code == "export_busy"


def test_failed_processing_remains_exportable_without_requeue(account):
    with SessionLocal.begin() as db:
        db.get(File, account.files[2]).status = "processing"
        db.add(Job(owner_id=account.owner, kind="image.process", dedupe_key=f"image_{account.files[2]}",
                   status="failed", error_code="worker_attempts_exhausted", attempts=3))
    manifest, data, _ = read_archive(account)
    file = next(f for f in data["files"] if f["id"] == str(account.files[2]))
    assert file["processing_status"] == "failed"
    assert file["processing_error_code"] == "worker_attempts_exhausted"
    assert manifest["resume_processing"] is False
    with SessionLocal() as db:
        assert db.scalar(select(Job)).status == "failed"


def test_worker_rejected_image_with_removed_original_does_not_block_export(client, account):
    from app.worker import tick

    login(client)
    target = note(client)
    item, raw = upload(client, target, data=b"this is not a valid image")
    assert put(client, item, raw).status_code == 200
    assert complete(client, item, 1).status_code == 200
    assert tick()
    with SessionLocal() as db:
        rejected = db.get(File, uuid.UUID(item["file"]["id"]))
        assert rejected.status == "rejected" and rejected.error_code
        with pytest.raises(FileNotFoundError):
            account.store.read(rejected.object_key)
    _, data, _ = read_archive(account)
    exported = next(f for f in data["files"] if f["id"] == item["file"]["id"])
    assert exported["status"] == "rejected" and exported["error_code"]
    assert exported["original_path"] is None
    assert exported["original_unavailable_reason"] == "image_rejected"


@pytest.mark.parametrize("change", ["revoked", "expired", "disabled", "wrong_owner"])
def test_invalid_current_session_rejects_export(account, change):
    with SessionLocal.begin() as db:
        session = db.get(AuthSession, account.session)
        if change == "revoked":
            session.revoked_at = utcnow()
        elif change == "expired":
            session.expires_at = utcnow() - timedelta(seconds=1)
        elif change == "disabled":
            db.get(User, account.owner).status = "disabled"
        else:
            session.user_id = account.outsider
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 401


def test_session_revoked_during_storage_read_aborts_and_closes_tempfile(account, monkeypatch):
    opened = []
    actual_tempfile = service.tempfile.TemporaryFile

    def track(*args, **kwargs):
        stream = actual_tempfile(*args, **kwargs)
        opened.append(stream)
        return stream

    actual_read = account.store.read

    def revoke(key):
        with SessionLocal.begin() as db:
            db.get(AuthSession, account.session).revoked_at = utcnow()
        return actual_read(key)

    monkeypatch.setattr(service.tempfile, "TemporaryFile", track)
    monkeypatch.setattr(account.store, "read", revoke)
    monkeypatch.setattr(service.files, "storage", lambda: account.store)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 401
    assert len(opened) == 1 and opened[0].closed


def test_timeout_after_slow_object_read_aborts(account, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    actual_read = account.store.read

    def slow(key):
        clock[0] = settings().export_max_seconds + 1
        return actual_read(key)

    monkeypatch.setattr(account.store, "read", slow)
    monkeypatch.setattr(service.files, "storage", lambda: account.store)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 503 and error.value.code == "export_timeout"


def test_storage_error_is_sanitized_and_closes_tempfile(account, monkeypatch):
    opened = []
    actual_tempfile = service.tempfile.TemporaryFile

    def track(*args, **kwargs):
        stream = actual_tempfile(*args, **kwargs)
        opened.append(stream)
        return stream

    def fail(key):
        raise RuntimeError("private OSS signed-url credential must not leak")

    monkeypatch.setattr(service.tempfile, "TemporaryFile", track)
    monkeypatch.setattr(account.store, "head", fail)
    monkeypatch.setattr(service.files, "storage", lambda: account.store)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 503 and error.value.code == "storage_unavailable"
    assert "private" not in error.value.message
    assert opened[0].closed


@pytest.mark.parametrize("field", ["file_id", "object_key", "bytes", "number"])
def test_source_json_cannot_redirect_archive_reads(account, monkeypatch, field):
    with SessionLocal.begin() as db:
        run = db.get(AiRun, account.runs[0])
        source = json.loads(json.dumps(run.source))
        source["pages"][0][field] = (str(uuid.uuid4()) if field == "file_id" else
            "other-owner/private/object" if field == "object_key" else 999)
        run.source = source
    reads = []
    monkeypatch.setattr(account.store, "read", lambda key: reads.append(key))
    monkeypatch.setattr(service.files, "storage", lambda: account.store)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 409
    assert not reads


def test_snapshot_reads_consistent_note_and_versions_across_concurrent_commit(account):
    changed = []

    def change_after_folder_query(connection, cursor, statement, parameters, context, executemany):
        if "FROM folders" in statement and not changed:
            changed.append(True)
            with SessionLocal.begin() as db:
                note = db.get(Note, account.text)
                note.title = "快照建立后的修改"
                note.revision = 3
                db.add(NoteVersion(owner_id=account.owner, note_id=account.text, revision=3,
                                   snapshot={"title": note.title}))

    event.listen(engine, "after_cursor_execute", change_after_folder_query)
    try:
        _, data, _ = read_archive(account)
    finally:
        event.remove(engine, "after_cursor_execute", change_after_folder_query)
    assert changed
    assert next(n for n in data["notes"] if n["id"] == str(account.text))["title"] == "保存的 PDF 回答"
    assert [v["revision"] for v in data["note_versions"]] == [1]
    with SessionLocal() as db:
        assert db.get(Note, account.text).title == "快照建立后的修改"
        assert sorted(db.scalars(select(NoteVersion.revision).where(NoteVersion.note_id == account.text))) == [1, 3]


@pytest.mark.parametrize("missing", ["reading", "thumbnail", "pdf_page"])
def test_ready_assets_require_complete_variant_and_pdf_page_metadata(account, missing):
    with SessionLocal.begin() as db:
        if missing == "pdf_page":
            # Preserve the rows while making the declared original page sequence incomplete.
            db.get(NotePage, account.pages[0]).pdf_page_number = 2
        else:
            target = db.get(FileVariant, account.variants[0 if missing == "reading" else 1])
            db.delete(target)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 409 and error.value.code == "export_incomplete"


def test_snapshot_does_not_deadlock_a_single_connection_pool(account, monkeypatch):
    with SessionLocal() as db:
        url = db.bind.url
    single = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=0.25)
    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=single, expire_on_commit=False))
    try:
        manifest, _, _ = read_archive(account)
        assert manifest["owner"]["id"] == str(account.owner)
    finally:
        single.dispose()


def test_database_exception_is_sanitized(account, monkeypatch):
    def fail():
        raise SQLAlchemyError("private DB URL credential")

    monkeypatch.setattr(service, "SessionLocal", fail)
    with pytest.raises(AppError) as error:
        service.create_archive(account.owner, account.session)
    assert error.value.status == 503 and error.value.code == "export_unavailable"
    assert "private" not in error.value.message
