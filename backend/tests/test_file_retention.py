"""Reachability is conservative, private, deterministic, and independent of access."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.modules.ai.models import AiConversation, AiRun, AiSavedNote
from app.modules.files.models import File, FileVariant, NotePage, PdfImport, UploadSession
from app.modules.files.retention import RetentionError, collect_references
from app.modules.notes.models import Note, NoteVersion
from app.shared.db import SessionLocal, engine, utcnow
from app.shared.models import Job, User


def body(value):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}


@pytest.fixture
def references():
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User).where(User.username == "admin")).id
        image = Note(owner_id=owner, title="图片资料", kind="image", deleted_at=utcnow(), revision=2)
        pdf = Note(owner_id=owner, title="PDF资料", kind="image")
        original_text = Note(owner_id=owner, title="当前已改名", revision=2, deleted_at=utcnow())
        saved_image = Note(owner_id=owner, title="保存图片回答", deleted_at=utcnow())
        saved_text = Note(owner_id=owner, title="保存文字回答")
        db.add_all([image, pdf, original_text, saved_image, saved_text])
        db.flush()
        all_keys, images, page_rows = set(), [], []
        for parent in (image, pdf):
            file_id = uuid4()
            key = f"{owner}/{file_id}/original"
            item = File(id=file_id, owner_id=owner, object_key=key, filename="page.jpg",
                        mime="image/jpeg", expected_bytes=20, bytes=20, status="ready")
            db.add(item)
            db.flush()
            page = NotePage(owner_id=owner, note_id=parent.id, file_id=file_id, position=0,
                            pdf_page_number=1 if parent is pdf else None)
            db.add(page)
            db.flush()
            reading = f"{owner}/{file_id}/current-reading.jpg"
            thumbnail = f"{owner}/{file_id}/thumbnail.jpg"
            db.add_all([FileVariant(file_id=file_id, kind="reading", object_key=reading, bytes=10),
                        FileVariant(file_id=file_id, kind="thumbnail", object_key=thumbnail, bytes=5)])
            snapshot = {"id": str(parent.id), "owner_id": str(owner), "kind": "image", "revision": 1,
                        "pages": [{"id": str(page.id), "file_id": str(file_id), "position": 0, "revision": 1}]}
            db.add(NoteVersion(owner_id=owner, note_id=parent.id, revision=1, snapshot=snapshot))
            all_keys.update((key, reading, thumbnail))
            images.append((item, reading))
            page_rows.append(page)

        pdf_id = uuid4()
        pdf_key = f"{owner}/{pdf_id}/original"
        pdf_original = File(id=pdf_id, owner_id=owner, object_key=pdf_key, filename="source.pdf",
                            mime="application/pdf", expected_bytes=30, bytes=30, status="ready")
        db.add(pdf_original)
        db.flush()
        imported = PdfImport(owner_id=owner, note_id=pdf.id, source_file_id=pdf_id,
                             request_hash="0" * 64, status="ready", page_count=1, completed_pages=1)
        upload = UploadSession(owner_id=owner, note_id=pdf.id, file_id=pdf_id, request_key="retention-pdf",
                               status="completed", expires_at=utcnow() - timedelta(days=1))
        db.add_all([imported, upload])
        all_keys.add(pdf_key)
        for state in ("expired", "rejected"):
            file_id = uuid4()
            key = f"{owner}/{file_id}/original"
            db.add(File(id=file_id, owner_id=owner, object_key=key, filename=f"{state}.png",
                        mime="image/png", expected_bytes=10, status=state))
            all_keys.add(key)

        selected_text = "未选择的原文🙂所选资料后文"
        quote = "🙂所选资料"
        start = selected_text.index(quote)
        text_snapshot = {"id": str(original_text.id), "owner_id": str(owner), "kind": "text", "revision": 1,
                         "title": "历史原文", "body_json": body(selected_text)}
        text_version = NoteVersion(owner_id=owner, note_id=original_text.id, revision=1, snapshot=text_snapshot)
        db.add(text_version)
        parent = AiConversation(owner_id=owner, request_key=uuid4())
        db.add(parent)
        db.flush()
        historical_key = f"{owner}/{images[1][0].id}/historical-reading.jpg"
        pdf_source = {"note_id": str(pdf.id), "title": pdf.title, "revision": 1, "mode": "pages",
                      "pages": [{"page_id": str(page_rows[1].id), "file_id": str(images[1][0].id),
                                 "number": 1, "object_key": historical_key, "bytes": 10}]}
        image_source = {"kind": "image", "note_id": str(image.id), "title": image.title, "revision": 1,
                        "mode": "region", "pages": [{"page_id": str(page_rows[0].id),
                            "file_id": str(images[0][0].id), "number": 1, "page_revision": 1,
                            "object_key": images[0][1], "bytes": 10,
                            "region": {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}}]}
        text_source = {"kind": "text", "note_id": str(original_text.id), "title": "历史原文", "revision": 1,
                       "start": start, "end": start + len(quote), "quote": quote}
        runs = []
        for source in (pdf_source, image_source, text_source):
            run = AiRun(owner_id=owner, conversation_id=parent.id, request_key=uuid4(), request_hash="0" * 64,
                        model="synthetic", prompt="", input_messages=[], answer="answer", status="succeeded", source=source)
            db.add(run)
            db.flush()
            runs.append(run)
        public_image = deepcopy(image_source)
        for name in ("file_id", "page_revision", "object_key", "bytes"):
            public_image["pages"][0].pop(name)
        db.add_all([AiSavedNote(owner_id=owner, run_id=runs[1].id, note_id=saved_image.id,
                               source=public_image, cited_page_numbers=[1]),
                    AiSavedNote(owner_id=owner, run_id=runs[2].id, note_id=saved_text.id,
                               source=text_source, cited_page_numbers=[])])
        job = Job(owner_id=owner, kind="image.process", status="succeeded", dedupe_key="retention-image",
                  payload={"file_id": str(images[0][0].id)}, result={"file_id": str(images[0][0].id), "accepted": True})
        db.add(job)
        db.flush()
        all_keys.add(historical_key)
        return SimpleNamespace(owner=owner, keys=all_keys, historical_key=historical_key, runs=[r.id for r in runs],
                               image=image.id, pdf=pdf.id, pages=[p.id for p in page_rows], file=images[0][0].id,
                               text_version=text_version.id, upload=upload.id, imported=imported.id, job=job.id)


def collect():
    with engine.connect() as connection:
        return collect_references(connection)


def test_all_registered_and_historical_keys_survive_trash_expiry_and_rejection(references):
    result = collect()
    assert result["keys"] == references.keys
    assert references.historical_key in result["keys"]
    assert result["active_writers"] == 0
    assert result["counts"]["text_source_versions"] == 1
    assert len(result["fingerprint"]) == 64
    assert result == collect()
    assert set(result) == {"keys", "counts", "active_writers", "fingerprint"}
    assert "所选" not in repr(result) and "历史原文" not in repr(result)


def test_historical_visual_page_identity_is_retained_after_current_page_disappears(references):
    with SessionLocal.begin() as db:
        db.delete(db.get(NotePage, references.pages[1]))
    assert references.historical_key in collect()["keys"]


@pytest.mark.parametrize("kind", ["unknown_source", "bad_page", "bad_region", "bad_text", "bad_version", "foreign_version", "foreign_key"])
def test_damaged_or_foreign_reference_aborts_entire_collection(references, kind):
    with SessionLocal.begin() as db:
        if kind in {"unknown_source", "bad_page", "bad_region"}:
            run = db.get(AiRun, references.runs[1])
            value = deepcopy(run.source)
            if kind == "unknown_source":
                value["kind"] = "future-unknown-source"
            elif kind == "bad_page":
                value["pages"][0]["object_key"] = "../foreign/key"
            else:
                value["pages"][0]["region"]["width"] = 2
            run.source = value
        elif kind == "bad_text":
            run = db.get(AiRun, references.runs[2])
            run.source = {**run.source, "quote": "不同的选区"}
        elif kind == "foreign_key":
            outsider = User(username="other", display_name="其他人", password_hash="synthetic")
            db.add(outsider)
            db.flush()
            foreign_id = uuid4()
            foreign_key = f"{outsider.id}/{foreign_id}/original"
            db.add(File(id=foreign_id, owner_id=outsider.id, object_key=foreign_key, filename="other.jpg",
                        mime="image/jpeg", expected_bytes=10, bytes=10, status="ready"))
            run = db.get(AiRun, references.runs[0])
            value = deepcopy(run.source)
            value["pages"][0]["object_key"] = foreign_key
            run.source = value
        else:
            version = db.get(NoteVersion, references.text_version)
            version.snapshot = {**version.snapshot, "owner_id": str(uuid4())} if kind == "foreign_version" else []
    with pytest.raises(RetentionError) as error:
        collect()
    assert error.value.code == "retention_reference_invalid"
    assert str(error.value) == error.value.code


@pytest.mark.parametrize("kind", ["job", "stale_failed_job", "ai", "upload", "pdf"])
def test_active_or_uncertain_writer_is_counted_even_with_old_leases(references, kind):
    with SessionLocal.begin() as db:
        if kind in {"job", "stale_failed_job"}:
            row = db.get(Job, references.job)
            row.status = "queued" if kind == "job" else "failed"
            if kind == "stale_failed_job":
                row.lease_until, row.worker_token = utcnow() - timedelta(days=1), "old-worker-token"
        elif kind == "ai":
            db.get(AiRun, references.runs[0]).status = "queued"
        elif kind == "upload":
            db.get(UploadSession, references.upload).status = "received"
        else:
            db.get(PdfImport, references.imported).status = "processing"
    assert collect()["active_writers"] > 0


def test_expired_pdf_upload_is_retained_without_blocking_and_reactivation_blocks(references):
    with SessionLocal.begin() as db:
        parent = Note(owner_id=references.owner, title="过期的PDF", kind="image")
        db.add(parent)
        db.flush()
        source_id = uuid4()
        key = f"{references.owner}/{source_id}/original"
        db.add(File(id=source_id, owner_id=references.owner, object_key=key, filename="expired.pdf",
                    mime="application/pdf", expected_bytes=10, status="expired"))
        db.flush()
        upload = UploadSession(owner_id=references.owner, note_id=parent.id, file_id=source_id,
                               request_key="expired-pdf-reservation", status="expired",
                               expires_at=utcnow() - timedelta(days=1))
        db.add_all([upload, PdfImport(owner_id=references.owner, note_id=parent.id,
                                     source_file_id=source_id, request_hash="1" * 64, status="uploading")])
        db.flush()
        upload_id = upload.id
    result = collect()
    assert result["keys"] == references.keys | {key}
    assert result["active_writers"] == 0
    with SessionLocal.begin() as db:
        db.get(UploadSession, upload_id).status = "pending"
        db.get(File, source_id).status = "uploading"
    reactivated = collect()
    assert reactivated["active_writers"] > 0
    assert reactivated["fingerprint"] != result["fingerprint"]


def test_pdf_import_without_its_upload_chain_is_not_safe_to_collect(references):
    with SessionLocal.begin() as db:
        db.delete(db.get(UploadSession, references.upload))
    with pytest.raises(RetentionError):
        collect()


def test_fingerprint_changes_on_references_and_restorable_identity_changes(references):
    first = collect()
    with SessionLocal.begin() as db:
        row = db.get(Note, references.image)
        row.deleted_at = None
        row.revision += 1
    second = collect()
    assert first["keys"] == second["keys"] and first["fingerprint"] != second["fingerprint"]
    with SessionLocal.begin() as db:
        row = db.get(AiRun, references.runs[0])
        value = deepcopy(row.source)
        value["pages"][0]["object_key"] += ".old"
        row.source = value
    third = collect()
    assert second["fingerprint"] != third["fingerprint"]
    assert references.historical_key + ".old" in third["keys"]


def test_queries_never_read_auth_secrets_input_messages_or_unrelated_note_bodies(references):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        collect()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert all(statement.lstrip().startswith("select") for statement in statements)
    joined = "\n".join(statements)
    assert all(name not in joined for name in ("password_hash", "token_hash", "ciphertext", "input_messages", "request_hash"))
    assert "select snapshot->'title'" in joined
    assert "select id,owner_id,kind,revision,deleted_at from public.notes" in joined
    assert "select *" not in joined
