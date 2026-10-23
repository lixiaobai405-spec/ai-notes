from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.modules.files import pdf_service as service, schemas as s
from app.modules.files.models import PdfImport
from app.modules.files.oss_storage import StorageUnavailable
from app.shared.dependencies import CurrentUser, DB
from app.shared.errors import AppError

router = APIRouter(tags=["files"])


@router.post("/pdf-imports", response_model=s.PdfImportOut, status_code=201)
def create_import(data: s.PdfImportCreate, db: DB, user: CurrentUser):
    item = service.create(db, user, data)
    db.commit()
    return service.output(db, item)


@router.get("/pdf-imports/{import_id}", response_model=s.PdfImportOut)
def get_import(import_id: UUID, db: DB, user: CurrentUser):
    return service.output(db, service.owned(db, user, import_id))


@router.get("/notes/{note_id}/pdf-imports", response_model=list[s.PdfImportOut])
def note_imports(note_id: UUID, db: DB, user: CurrentUser):
    service.files.image_note(db, user, note_id)
    return [service.output(db, item) for item in db.scalars(select(PdfImport).where(
        PdfImport.owner_id == user.id, PdfImport.note_id == note_id))]


@router.post("/pdf-imports/{import_id}/start", response_model=s.PdfImportOut)
def start_import(import_id: UUID, db: DB, user: CurrentUser):
    try:
        item = service.start(db, user, import_id)
    except (StorageUnavailable, FileNotFoundError):
        raise AppError(503, "storage_unavailable", "PDF 存储暂不可用，请稍后重试") from None
    db.commit()
    return service.output(db, item)


@router.post("/pdf-imports/{import_id}/retry", response_model=s.PdfImportOut)
def retry_import(import_id: UUID, db: DB, user: CurrentUser):
    item = service.retry(db, user, import_id)
    db.commit()
    return service.output(db, item)
