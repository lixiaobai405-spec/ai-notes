from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UploadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(strict=True, gt=0)
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    filename: str
    mime: str
    bytes: int | None
    width: int | None
    height: int | None
    status: Literal["uploading", "processing", "ready", "rejected", "expired", "failed"]
    error_code: str | None


class UploadOut(BaseModel):
    id: UUID
    file: FileOut
    status: str
    expires_at: datetime
    upload_url: str
    upload_method: str


class PageOut(BaseModel):
    id: UUID
    note_id: UUID
    file: FileOut
    position: int
    revision: int


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=1)


class CompleteOut(BaseModel):
    page: PageOut
    note_revision: int


class ReorderIn(CompleteIn):
    page_ids: list[UUID] = Field(min_length=1, max_length=500)


class TicketOut(BaseModel):
    url: str
    expires_at: datetime


class PdfImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(strict=True, gt=0)
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    notebook_id: UUID | None = None
    tag_ids: list[UUID] = Field(default_factory=list, max_length=100)


class PdfImportOut(BaseModel):
    id: UUID
    note_id: UUID
    filename: str
    status: Literal["uploading", "expired", "queued", "processing", "ready", "rejected", "failed"]
    page_count: int | None
    completed_pages: int
    error_code: str | None
    upload: UploadOut
