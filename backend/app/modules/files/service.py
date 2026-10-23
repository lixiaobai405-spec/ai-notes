from datetime import timedelta
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.modules.files import schemas as s
from app.modules.files.images import InvalidImage, process_image
from app.modules.files.models import File, FileTicket, FileVariant, NotePage, UploadSession
from app.modules.files.storage import LocalStorage
from app.modules.files.oss_storage import OSSStorage, StorageUnavailable
from app.modules.notes import service as notes
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.errors import AppError
from app.shared.models import Job, Outbox, User
from app.shared.permissions import require_owner
from app.shared.security import digest, token


@lru_cache(maxsize=1)
def _oss_storage():
    return OSSStorage(settings())


def storage():
    cfg = settings()
    return _oss_storage() if cfg.storage_backend == "oss" else LocalStorage(cfg.storage_root)


def image_note(db, user, note_id):
    note = notes.owned(db, user, Note, note_id)
    if note.kind != "image":
        raise AppError(422, "image_note_required", "请在图片笔记中上传页面")
    return note


def owned_upload(db, user, upload_id):
    upload = require_owner(user, db.scalar(select(UploadSession).where(
        UploadSession.id == upload_id).with_for_update()))
    image_note(db, user, upload.note_id)
    if upload.expires_at <= utcnow() and upload.status != "completed":
        raise AppError(410, "upload_expired", "上传已过期，请重新选择文件")
    return upload


def file_output(db, file):
    result = s.FileOut.model_validate(file)
    if file.status == "processing":
        job = db.scalar(select(Job).where(Job.owner_id == file.owner_id,
                        Job.kind == "image.process", Job.dedupe_key == f"image_{file.id}"))
        if job and job.status == "failed":
            return result.model_copy(update={"status": "failed", "error_code": job.error_code})
    return result


def upload_output(db, upload):
    return s.UploadOut(
        id=upload.id, file=file_output(db, db.get(File, upload.file_id)), status=upload.status,
        expires_at=upload.expires_at,
        upload_url=f"/api/v1/uploads/{upload.id}/content", upload_method="PUT",
    )


def page_output(db, page):
    return s.PageOut(id=page.id, note_id=page.note_id, file=file_output(db, db.get(File, page.file_id)),
                     position=page.position, revision=page.revision)


def create_upload(db, user, note_id, data):
    image_note(db, user, note_id)
    from app.modules.files.pdf_service import require_idle
    require_idle(db, note_id)
    cfg = settings()
    storage()  # Fail before reserving data if the selected adapter is not ready.
    if data.size > cfg.upload_max_bytes:
        raise AppError(413, "file_too_large", "单张图片超过上传上限")
    if "\x00" in data.filename or any(0xD800 <= ord(c) <= 0xDFFF for c in data.filename):
        raise AppError(422, "filename_invalid", "文件名格式不正确")
    existing = db.scalar(select(UploadSession).where(
        UploadSession.owner_id == user.id, UploadSession.request_key == data.request_key))
    if existing:
        original = db.get(File, existing.file_id)
        if (existing.note_id, original.filename, original.mime, original.expected_bytes) != (
                note_id, data.filename, data.mime, data.size):
            raise AppError(409, "request_key_conflict", "同一上传标识不能用于不同文件")
        return existing
    active = db.scalar(select(func.count()).select_from(UploadSession).where(
        UploadSession.owner_id == user.id, UploadSession.status.in_(["pending", "received"]),
        UploadSession.expires_at > utcnow()))
    if active >= cfg.upload_batch_limit:
        raise AppError(429, "upload_limit", "在途上传过多，请等待完成后再继续")
    used = db.scalar(select(func.coalesce(func.sum(File.expected_bytes), 0)).where(
        File.owner_id == user.id, File.status.in_(["uploading", "processing", "ready"])))
    if used + data.size > cfg.storage_quota_bytes:
        raise AppError(413, "storage_quota", "图片原文件容量已达到上限")
    file_id = uuid4()
    file = File(id=file_id, owner_id=user.id, object_key=f"{user.id}/{file_id}/original",
                filename=data.filename, mime=data.mime, expected_bytes=data.size)
    db.add(file)
    db.flush()
    upload = UploadSession(owner_id=user.id, file_id=file.id, note_id=note_id,
                           request_key=data.request_key,
                           expires_at=utcnow() + timedelta(seconds=cfg.upload_expiry_seconds))
    db.add(upload)
    db.flush()
    return upload


def receive_upload(db, user, upload_id, data, mime):
    upload = owned_upload(db, user, upload_id)
    file = db.get(File, upload.file_id)
    if upload.status == "completed":
        raise AppError(409, "upload_completed", "已完成的原图不能覆盖")
    if mime != file.mime or len(data) != file.expected_bytes:
        raise AppError(422, "upload_mismatch", "文件大小或类型与上传声明不一致")
    if upload.status == "received":
        if storage().read(file.object_key) != data:
            raise AppError(409, "upload_content_conflict", "上传内容已经确认，请使用新的上传标识")
        return upload
    storage().put(file.object_key, data)
    upload.status = "received"
    file.bytes = len(data)
    return upload


def complete_upload(db, user, upload_id, revision):
    upload = owned_upload(db, user, upload_id)
    note = image_note(db, user, upload.note_id)
    from app.modules.files.pdf_service import require_idle
    if db.get(File, upload.file_id).mime == "application/pdf":
        raise AppError(422, "pdf_import_required", "请通过 PDF 导入接口开始转换")
    require_idle(db, note.id)
    if upload.status == "completed":
        page = db.scalar(select(NotePage).where(NotePage.note_id == note.id, NotePage.file_id == upload.file_id))
        return page, note.revision
    notes.revision_matches(note, revision)
    if upload.status != "received":
        raise AppError(409, "upload_incomplete", "文件尚未上传完成")
    file = db.get(File, upload.file_id)
    try:
        actual = storage().head(file.object_key)
    except FileNotFoundError:
        raise AppError(409, "upload_incomplete", "上传文件未找到，请重试") from None
    except StorageUnavailable:
        raise AppError(503, "storage_unavailable", "文件存储暂不可用，请稍后重试") from None
    if actual.size != file.expected_bytes:
        raise AppError(422, "upload_mismatch", "文件实际大小与声明不一致")
    last = db.scalar(select(func.max(NotePage.position)).where(NotePage.note_id == note.id))
    page = NotePage(owner_id=user.id, note_id=note.id, file_id=file.id,
                    position=0 if last is None else last + 1)
    db.add(page)
    file.status = "processing"
    upload.status = "completed"
    db.add(Job(owner_id=user.id, kind="image.process", dedupe_key=f"image_{file.id}",
               payload={"file_id": str(file.id)}, status="queued", max_attempts=3))
    notes.revise(db, note, revision, {})
    db.flush()
    notes.save_snapshot(db, note)
    return page, note.revision


def list_pages(db, user, note_id):
    image_note(db, user, note_id)
    return db.scalars(select(NotePage).where(NotePage.owner_id == user.id,
                                            NotePage.note_id == note_id).order_by(NotePage.position)).all()


def reorder_pages(db, user, note_id, data):
    note = image_note(db, user, note_id)
    from app.modules.files.pdf_service import require_idle
    require_idle(db, note_id)
    notes.revision_matches(note, data.revision)
    pages = list_pages(db, user, note_id)
    if len(set(data.page_ids)) != len(data.page_ids) or set(data.page_ids) != {p.id for p in pages}:
        raise AppError(422, "page_order_invalid", "排序必须包含这篇笔记的全部页面且不重复")
    by_id = {p.id: p for p in pages}
    # Move into a free range first; immediate unique constraints stay valid at each UPDATE.
    start = max((p.position for p in pages), default=0) + 1
    for index, page in enumerate(pages):
        page.position = start + index
    db.flush()
    for index, page_id in enumerate(data.page_ids):
        by_id[page_id].position = index
        by_id[page_id].revision += 1
    notes.revise(db, note, data.revision, {})
    db.flush()
    notes.save_snapshot(db, note)
    return note.revision


def linked_file(db, user, file_id):
    file = require_owner(user, db.get(File, file_id))
    # A file is readable only through a live private note, including after trashing it.
    linked = db.scalar(select(NotePage.id).join(Note, NotePage.note_id == Note.id).where(
        NotePage.file_id == file.id, NotePage.owner_id == user.id,
        Note.owner_id == user.id, Note.deleted_at.is_(None)).limit(1))
    if not linked:
        from app.modules.files.models import PdfImport
        linked = db.scalar(select(PdfImport.id).join(Note, PdfImport.note_id == Note.id).where(
            PdfImport.source_file_id == file.id, PdfImport.owner_id == user.id,
            Note.owner_id == user.id, Note.deleted_at.is_(None)).limit(1))
        if not linked:
            raise AppError(404, "not_found", "文件不存在或无权访问")
    return file


def accessible_file(db, user, file_id):
    file = linked_file(db, user, file_id)
    if file.status != "ready":
        raise AppError(409, "file_not_ready", "图片仍在处理或未通过校验")
    return file


def retry_file(db, user, file_id):
    file = linked_file(db, user, file_id)
    job = db.scalar(select(Job).where(Job.owner_id == user.id, Job.kind == "image.process",
                    Job.dedupe_key == f"image_{file.id}").with_for_update())
    if not job or job.status != "failed" or file.status != "processing":
        raise AppError(409, "retry_unavailable", "仅处理失败的图片可以重试")
    job.status, job.attempts, job.run_after = "queued", 0, utcnow()
    job.worker_token, job.lease_until, job.error_code = None, None, None
    return file


def create_ticket(db, user, file_id, kind):
    file = accessible_file(db, user, file_id)
    if file.mime == "application/pdf" and kind != "original":
        raise AppError(422, "pdf_original_only", "PDF 原件仅支持下载原文件")
    raw = token()
    expires = utcnow() + timedelta(seconds=settings().file_ticket_seconds)
    db.add(FileTicket(owner_id=user.id, file_id=file_id, token_hash=digest(raw), expires_at=expires))
    return {"url": f"/api/v1/files/{file_id}/content?ticket={raw}&kind={kind}", "expires_at": expires}


def read_ticket(db, user, file_id, raw, kind):
    file = accessible_file(db, user, file_id)
    ticket = db.scalar(select(FileTicket).where(
        FileTicket.owner_id == user.id, FileTicket.file_id == file_id,
        FileTicket.token_hash == digest(raw), FileTicket.expires_at > utcnow()))
    if not ticket:
        raise AppError(403, "file_ticket_invalid", "图片链接已过期，请重新打开")
    key, mime = file.object_key, file.mime
    if kind != "original":
        variant = db.scalar(select(FileVariant).where(FileVariant.file_id == file_id, FileVariant.kind == kind))
        if not variant:
            raise AppError(404, "not_found", "图片变体暂不可用")
        key, mime = variant.object_key, "image/jpeg"
    try:
        return storage().read(key), mime
    except (FileNotFoundError, StorageUnavailable):
        raise AppError(503, "file_unavailable", "图片文件暂不可用") from None


def _discard_unpublished_variants(file_id, written):
    if not written:
        return
    keys = [key for _, key, _ in written]
    try:
        # Wait for an uncertain publication transaction to finish before reading
        # its references. A plain SELECT could miss still-uncommitted variants.
        with SessionLocal() as db, db.begin():
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.scalar(select(File.id).where(File.id == file_id).with_for_update())
            # File exists before processing; only exact variant keys prove publication.
            referenced = set(db.scalars(select(FileVariant.object_key).where(FileVariant.object_key.in_(keys))))
    except Exception:
        return  # Unknown database state: retain objects rather than risk data loss.
    for key in keys:
        if key not in referenced:
            try:
                storage().delete(key)
            except Exception:
                pass  # Cleanup must not replace the handler's original failure.


def process_job(claimed):
    from uuid import UUID

    file_id = UUID(claimed["payload"]["file_id"])
    with SessionLocal() as db:
        file = db.get(File, file_id)
        if file is None:
            raise ValueError("file_missing")
        key, mime = file.object_key, file.mime
    written = []
    result = None
    try:
        try:
            original = storage().read(key)
            if len(original) > settings().upload_max_bytes:
                raise InvalidImage("file_too_large")
            result = process_image(original, mime, settings().upload_max_pixels)
            # Token-qualified keys prevent an expired worker from replacing a new worker's output.
            prefix = key.rsplit("/", 1)[0]
            for kind, data in (("reading", result.reading), ("thumbnail", result.thumbnail)):
                variant_key = f"{prefix}/{claimed['token']}-{kind}.jpg"
                # A provider can persist an object and then lose the PUT response.
                written.append((kind, variant_key, len(data)))
                storage().put(variant_key, data)
            error = None
        except InvalidImage as exc:
            error = str(exc)
        with SessionLocal.begin() as db:
            job = db.scalar(select(Job).where(Job.id == claimed["id"]).with_for_update())
            if (not job or job.status != "running" or job.worker_token != claimed["token"]
                    or job.lease_until <= utcnow()):
                return False
            file = db.scalar(select(File).where(File.id == file_id).with_for_update())
            if error:
                file.status, file.error_code = "rejected", error
            else:
                file.status, file.error_code = "ready", None
                file.sha256, file.width, file.height = result.sha256, result.width, result.height
                for kind, object_key, size in written:
                    db.execute(insert(FileVariant).values(id=uuid4(), file_id=file.id, kind=kind,
                               object_key=object_key, bytes=size, created_at=utcnow()).on_conflict_do_nothing(
                                   constraint="uq_file_variant_kind"))
            job.status, job.result = "succeeded", {"file_id": str(file.id), "accepted": error is None}
            job.error_code, job.worker_token, job.lease_until = None, None, None
            db.add(Outbox(user_id=file.owner_id, job_id=job.id, kind="image.processed",
                          payload={"message": "图片未通过校验，请检查文件后重新上传" if error else "图片处理完成，可以阅读了"}))
        if error:
            storage().delete(key)
        return True
    finally:
        # Also discard keys skipped by a uniqueness conflict on a successful retry.
        _discard_unpublished_variants(file_id, written)


def maintenance():
    # Keep upload records for idempotency; expired state releases the original-byte reservation.
    with SessionLocal.begin() as db:
        expired = db.scalars(select(UploadSession).where(
            UploadSession.status.in_(["pending", "received"]), UploadSession.expires_at <= utcnow()
        ).with_for_update(skip_locked=True).limit(100)).all()
        for upload in expired:
            file = db.get(File, upload.file_id)
            storage().delete(file.object_key)
            file.status, upload.status = "expired", "expired"
        db.execute(delete(FileTicket).where(FileTicket.expires_at <= utcnow()))
