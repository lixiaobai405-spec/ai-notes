"""Authenticated, CSRF-protected downloads; archives never become public objects."""

from threading import BoundedSemaphore

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.modules.exports import service
from app.shared.db import utcnow
from app.shared.dependencies import CurrentUser, DB
from app.shared.errors import AppError

router = APIRouter(tags=["exports"])
# Bound temporary disk usage per API process, including a slow active download.
_downloads = BoundedSemaphore(1)


class ArchiveResponse(StreamingResponse):
    def __init__(self, archive, filename):
        self.archive = archive
        archive.seek(0, 2)
        size = archive.tell()
        archive.seek(0)
        super().__init__(self.chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        })

    def chunks(self):
        while chunk := self.archive.read(256 * 1024):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.archive.close()
            finally:
                _downloads.release()


@router.post("/exports/archive", response_class=StreamingResponse, responses={
    200: {"description": "完整的当前账号归档", "content": {
        "application/zip": {"schema": {"type": "string", "format": "binary"}},
    }},
})
def export_archive(request: Request, db: DB, user: CurrentUser):
    owner_id, session_id = user.id, request.state.auth_session.id
    # Authentication holds the owner lock for POST. Release it before storage I/O.
    db.rollback()
    if not _downloads.acquire(blocking=False):
        raise AppError(429, "export_busy", "当前有归档正在生成或下载，请稍后重试")
    archive = None
    try:
        archive = service.create_archive(owner_id, session_id)
        filename = "ai-notes-export-" + utcnow().strftime("%Y%m%dT%H%M%SZ") + ".zip"
        return ArchiveResponse(archive, filename)
    except BaseException:
        try:
            if archive is not None:
                archive.close()
        finally:
            _downloads.release()
        raise
