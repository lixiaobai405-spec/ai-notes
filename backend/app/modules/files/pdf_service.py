from datetime import timedelta
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.modules.files import schemas as s, service as files
from app.modules.files.images import process_image
from app.modules.files.models import File, FileVariant, NotePage, PdfImport, UploadSession
from app.modules.files.pdf_render import InvalidPdf, PdfRenderFailure, render_pdf
from app.modules.jobs import service as jobs
from app.modules.notes import schemas as ns, service as notes
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.errors import AppError
from app.shared.models import Job, Outbox, User
from app.shared.permissions import require_owner


def owned(db, user, import_id):
    item = require_owner(user, db.get(PdfImport, import_id))
    files.image_note(db, user, item.note_id)
    return item


def job_for(db, item):
    return db.scalar(select(Job).where(Job.owner_id == item.owner_id, Job.dedupe_key == f"pdf_{item.id}"))


def output(db, item):
    upload = db.scalar(select(UploadSession).where(UploadSession.file_id == item.source_file_id))
    status, error = item.status, item.error_code
    if status == "uploading" and (upload.status == "expired" or upload.expires_at <= utcnow()):
        status, error = "expired", "upload_expired"
    job = job_for(db, item)
    if status in {"queued", "processing"} and job and job.status == "failed":
        status, error = "failed", job.error_code
    return s.PdfImportOut(id=item.id, note_id=item.note_id, filename=db.get(File, item.source_file_id).filename,
                          status=status, error_code=error, page_count=item.page_count,
                          completed_pages=item.completed_pages, upload=files.upload_output(db, upload))


def used_bytes(db, owner_id):
    return db.scalar(select(func.coalesce(func.sum(File.expected_bytes), 0)).where(
        File.owner_id == owner_id, File.status.in_(["uploading", "processing", "ready"])))


def create(db, user, data):
    cfg = settings()
    if data.size > cfg.pdf_max_bytes:
        raise AppError(413, "pdf_too_large", "PDF 超过单文件上传上限")
    if (not data.filename.lower().endswith(".pdf") or "\x00" in data.filename
            or any(0xD800 <= ord(c) <= 0xDFFF for c in data.filename)):
        raise AppError(422, "pdf_filename_invalid", "请选择有效的 PDF 文件名")
    if len(set(data.tag_ids)) != len(data.tag_ids):
        raise AppError(422, "pdf_tags_invalid", "标签不能重复")
    fingerprint = hashlib.sha256(data.model_dump_json(exclude={"request_key"}).encode()).hexdigest()
    existing = db.scalar(select(UploadSession).where(UploadSession.owner_id == user.id,
                                                    UploadSession.request_key == data.request_key))
    if existing:
        item = db.scalar(select(PdfImport).where(PdfImport.source_file_id == existing.file_id))
        if not item or item.request_hash != fingerprint:
            raise AppError(409, "request_key_conflict", "同一导入标识不能用于不同文件或目标")
        return owned(db, user, item.id)
    pending = db.scalar(select(func.count()).select_from(PdfImport).join(Note, Note.id == PdfImport.note_id).where(
        PdfImport.owner_id == user.id, Note.deleted_at.is_(None), PdfImport.status.in_(["uploading", "queued", "processing"])))
    if pending >= 10:
        raise AppError(429, "pdf_import_limit", "待完成的 PDF 导入过多，请先完成现有任务")
    if used_bytes(db, user.id) + data.size > cfg.storage_quota_bytes:
        raise AppError(413, "storage_quota", "文件容量已达到上限")
    files.storage()
    note = notes.new_note(db, user, ns.NoteCreate(title=data.filename[:-4].strip() or "PDF 笔记",
                                                kind="image", notebook_id=data.notebook_id, tag_ids=data.tag_ids))
    file_id = uuid4()
    source = File(id=file_id, owner_id=user.id, object_key=f"{user.id}/{file_id}/original",
                  filename=data.filename, mime="application/pdf", expected_bytes=data.size)
    db.add(source)
    db.flush()
    upload = UploadSession(owner_id=user.id, file_id=file_id, note_id=note.id, request_key=data.request_key,
                           expires_at=utcnow() + timedelta(seconds=cfg.upload_expiry_seconds))
    item = PdfImport(owner_id=user.id, note_id=note.id, source_file_id=file_id, request_hash=fingerprint)
    db.add_all([upload, item])
    db.flush()
    return item


def start(db, user, import_id):
    item = owned(db, user, import_id)
    if item.status != "uploading":
        return item
    upload = db.scalar(select(UploadSession).where(UploadSession.file_id == item.source_file_id))
    files.owned_upload(db, user, upload.id)
    if upload.status != "received":
        raise AppError(409, "upload_incomplete", "PDF 尚未上传完成")
    source = db.get(File, item.source_file_id)
    if files.storage().head(source.object_key).size != source.expected_bytes:
        raise AppError(422, "upload_mismatch", "PDF 实际大小与声明不一致")
    source.status, upload.status, item.status = "ready", "completed", "queued"
    db.add(Job(owner_id=user.id, kind="pdf.import", dedupe_key=f"pdf_{item.id}",
               payload={"import_id": str(item.id)}, status="queued", max_attempts=3))
    db.flush()
    return item


def retry(db, user, import_id):
    item = owned(db, user, import_id)
    if item.status == "uploading":
        upload = db.scalar(select(UploadSession).where(UploadSession.file_id == item.source_file_id).with_for_update())
        source = db.get(File, item.source_file_id)
        if upload.status == "expired" or upload.expires_at <= utcnow():
            if source.status != "expired":
                # Maintenance may not have run yet; do not overwrite already-received bytes.
                upload.expires_at = utcnow() + timedelta(seconds=settings().upload_expiry_seconds)
                return item
            if used_bytes(db, user.id) + source.expected_bytes > settings().storage_quota_bytes:
                raise AppError(413, "storage_quota", "文件容量已达到上限")
            source.status, source.bytes, upload.status = "uploading", None, "pending"
        upload.expires_at = utcnow() + timedelta(seconds=settings().upload_expiry_seconds)
        return item
    job = job_for(db, item)
    if item.status not in {"processing", "queued"} or not job or job.status != "failed":
        raise AppError(409, "retry_unavailable", "仅失败的 PDF 转换可以重试")
    job.status, job.attempts, job.run_after = "queued", 0, utcnow()
    job.worker_token, job.lease_until, job.error_code = None, None, None
    item.error_code = None
    return item


def require_idle(db, note_id):
    active = db.scalar(select(PdfImport.id).where(PdfImport.note_id == note_id,
                        PdfImport.status.in_(["uploading", "queued", "processing"])))
    if active:
        raise AppError(409, "pdf_import_in_progress", "请先完成 PDF 导入，再上传图片或调整页序")


def _locked(db, claimed):
    item = db.get(PdfImport, UUID(claimed["payload"]["import_id"]))
    if not item:
        return None
    # Match the API lock order: owner before job/note. Never hold locks while rendering.
    user = db.scalar(select(User).where(User.id == item.owner_id).with_for_update())
    job = db.scalar(select(Job).where(Job.id == claimed["id"]).with_for_update())
    if (not job or job.status != "running" or job.worker_token != claimed["token"]
            or job.lease_until <= utcnow()):
        return None
    db.refresh(item)
    note = db.get(Note, item.note_id)
    if user.status != "active" or note.deleted_at is not None:
        job.status, job.error_code, job.worker_token, job.lease_until = "failed", "pdf_note_unavailable", None, None
        return None
    return item, job, note


def _complete(db, item, job, error=None):
    item.status, item.error_code = ("rejected", error) if error else ("ready", None)
    job.status, job.result = "succeeded", {"import_id": str(item.id), "pages": item.completed_pages, "accepted": error is None}
    job.worker_token, job.lease_until, job.error_code = None, None, None
    db.execute(insert(Outbox).values(id=uuid4(), user_id=item.owner_id, job_id=job.id, kind="pdf.imported",
        payload={"message": "PDF 转换未完成，请在笔记中查看原因" if error else "PDF 已逐页转换，可以阅读了"},
        created_at=utcnow()).on_conflict_do_nothing(index_elements=[Outbox.job_id]))


def _discard_unpublished(file_id, written):
    # A failed commit acknowledgement can hide a successful commit. Keep referenced
    # objects, and retain uncertain ones if the database is temporarily unreachable.
    try:
        with SessionLocal() as db:
            if db.get(File, file_id) is not None:
                return
    except SQLAlchemyError:
        return
    for _, object_key, _ in written:
        files.storage().delete(object_key)


def process_job(claimed):
    with SessionLocal.begin() as db:
        locked = _locked(db, claimed)
        if not locked:
            return False
        item, job, _ = locked
        if item.status == "ready":
            _complete(db, item, job)
            return True
        item.status = "processing"
        source = db.get(File, item.source_file_id)
        key, expected_bytes, start_page = source.object_key, source.expected_bytes, item.completed_pages
    cfg = settings()
    data = files.storage().read(key)
    renderer = None
    try:
        if len(data) != expected_bytes or len(data) > cfg.pdf_max_bytes or not data.startswith(b"%PDF-"):
            raise InvalidPdf("pdf_invalid")
        renderer = render_pdf(data, start_page, max_pages=cfg.pdf_max_pages,
                              timeout=cfg.pdf_page_timeout_seconds, max_bytes=cfg.upload_max_bytes,
                              max_pixels=cfg.upload_max_pixels)
        for event in renderer:
            if "page_count" in event:
                with SessionLocal.begin() as db:
                    locked = _locked(db, claimed)
                    if not locked:
                        return False
                    item, _, _ = locked
                    item.page_count = event["page_count"]
                    db.get(File, item.source_file_id).sha256 = hashlib.sha256(data).hexdigest()
                continue
            image = event["data"]
            processed = process_image(image, "image/jpeg", cfg.upload_max_pixels)
            file_id = uuid4()
            prefix = f"{item.owner_id}/{file_id}/{claimed['token']}"
            objects = [("original", image), ("reading", processed.reading), ("thumbnail", processed.thumbnail)]
            written = []
            published = False
            try:
                for kind, content in objects:
                    object_key = f"{prefix}-{kind}.jpg"
                    files.storage().put(object_key, content)
                    written.append((kind, object_key, len(content)))
                with SessionLocal.begin() as db:
                    locked = _locked(db, claimed)
                    if not locked:
                        return False
                    item, job, note = locked
                    if item.completed_pages != event["index"]:
                        return False
                    if used_bytes(db, item.owner_id) + len(image) > cfg.storage_quota_bytes:
                        _complete(db, item, job, "storage_quota")
                        return True
                    file = File(id=file_id, owner_id=item.owner_id, object_key=written[0][1],
                                filename=f"第{event['index'] + 1:04d}页.jpg", mime="image/jpeg", expected_bytes=len(image),
                                bytes=len(image), width=processed.width, height=processed.height,
                                sha256=processed.sha256, status="ready")
                    db.add(file)
                    db.flush()
                    db.add(NotePage(owner_id=item.owner_id, note_id=item.note_id, file_id=file_id,
                                    position=event["index"], pdf_page_number=event["index"] + 1))
                    db.add_all(FileVariant(file_id=file_id, kind=kind, object_key=object_key, bytes=size)
                               for kind, object_key, size in written[1:])
                    item.completed_pages += 1
                    notes.revise(db, note, note.revision, {})
                    db.flush()
                    notes.save_snapshot(db, note)
                published = True
            finally:
                if not published:
                    _discard_unpublished(file_id, written)
        with SessionLocal.begin() as db:
            locked = _locked(db, claimed)
            if not locked:
                return False
            item, job, _ = locked
            _complete(db, item, job)
        return True
    except InvalidPdf as error:
        with SessionLocal.begin() as db:
            locked = _locked(db, claimed)
            if not locked:
                return False
            item, job, _ = locked
            _complete(db, item, job, str(error))
        return True
    except PdfRenderFailure as error:
        jobs.fail(claimed["id"], claimed["token"], str(error))
        return False
    finally:
        if renderer is not None:
            renderer.close()
