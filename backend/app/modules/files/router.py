from typing import Literal
from uuid import UUID
from urllib.parse import quote
import asyncio

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.modules.files import schemas as s, service
from app.shared.config import settings
from app.shared.dependencies import CurrentUser, DB, current_user
from app.shared.errors import AppError
from app.modules.files.oss_storage import StorageUnavailable

router = APIRouter(tags=["files"])
VariantKind = Literal["original", "reading", "thumbnail"]


@router.post("/notes/{note_id}/uploads", response_model=s.UploadOut, status_code=201)
def upload(note_id: UUID, data: s.UploadCreate, db: DB, user: CurrentUser):
    row = service.create_upload(db, user, note_id, data)
    db.commit()
    return service.upload_output(db, row)


@router.get("/uploads/{upload_id}", response_model=s.UploadOut)
def upload_status(upload_id: UUID, db: DB, user: CurrentUser):
    return service.upload_output(db, service.owned_upload(db, user, upload_id))


@router.put("/uploads/{upload_id}/content", response_model=s.UploadOut)
async def upload_content(upload_id: UUID, request: Request, db: DB, user: CurrentUser):
    session = service.owned_upload(db, user, upload_id)
    expected = db.get(service.File, session.file_id).expected_bytes
    is_pdf = db.get(service.File, session.file_id).mime == "application/pdf"
    limit = settings().pdf_max_bytes if is_pdf else settings().upload_max_bytes
    # Do not hold the user's write lock while waiting for a slow browser upload.
    db.rollback()
    chunks, size = [], 0
    try:
        async with asyncio.timeout(max(300, settings().upload_read_timeout_seconds) if is_pdf else settings().upload_read_timeout_seconds):
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise AppError(413, "file_too_large", "文件超过上传上限")
                if size > expected:
                    raise AppError(422, "upload_mismatch", "文件大小与上传声明不一致")
                chunks.append(chunk)
    except TimeoutError:
        raise AppError(408, "upload_timeout", "上传超时，请检查网络后重试") from None

    def receive():
        # Session revocation and owner permissions are checked again after transport.
        verified = current_user(request, db)
        try:
            return service.receive_upload(db, verified, upload_id, b"".join(chunks),
                                          request.headers.get("content-type", ""))
        except StorageUnavailable:
            raise AppError(503, "storage_unavailable", "文件存储暂不可用，请稍后重试") from None

    row = await run_in_threadpool(receive)
    db.commit()
    return service.upload_output(db, row)


@router.post("/uploads/{upload_id}/complete", response_model=s.CompleteOut)
def complete(upload_id: UUID, data: s.CompleteIn, db: DB, user: CurrentUser):
    page, revision = service.complete_upload(db, user, upload_id, data.revision)
    db.commit()
    return {"page": service.page_output(db, page), "note_revision": revision}


@router.get("/notes/{note_id}/pages", response_model=list[s.PageOut])
def pages(note_id: UUID, db: DB, user: CurrentUser):
    return [service.page_output(db, page) for page in service.list_pages(db, user, note_id)]


@router.patch("/notes/{note_id}/pages/order", response_model=s.CompleteIn)
def page_order(note_id: UUID, data: s.ReorderIn, db: DB, user: CurrentUser):
    revision = service.reorder_pages(db, user, note_id, data)
    db.commit()
    return {"revision": revision}


@router.post("/files/{file_id}/ticket", response_model=s.TicketOut)
def ticket(file_id: UUID, db: DB, user: CurrentUser, kind: VariantKind = "reading"):
    output = service.create_ticket(db, user, file_id, kind)
    db.commit()
    return output


@router.post("/files/{file_id}/retry", response_model=s.FileOut)
def retry_file(file_id: UUID, db: DB, user: CurrentUser):
    file = service.retry_file(db, user, file_id)
    db.commit()
    return service.file_output(db, file)


@router.get("/files/{file_id}/content")
def content(file_id: UUID, db: DB, user: CurrentUser,
            ticket: str = Query(min_length=20, max_length=128), kind: VariantKind = "reading"):
    data, mime = service.read_ticket(db, user, file_id, ticket, kind)
    disposition = "inline"
    if mime == "application/pdf":
        filename = quote(db.get(service.File, file_id).filename, safe="")
        disposition = "attachment; filename=original.pdf; filename*=UTF-8''" + filename
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": disposition,
        "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
