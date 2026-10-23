import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey,
                        ForeignKeyConstraint, Integer, String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.models import Entity


class File(Entity, Base):
    __tablename__ = "files"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(256), unique=True)
    filename: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(64))
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_files_owner_id"),
        CheckConstraint("expected_bytes > 0 AND (bytes IS NULL OR bytes > 0)", name="file_bytes_positive"),
        CheckConstraint("status IN ('uploading','processing','ready','rejected','expired')", name="file_status"),
    )


class UploadSession(Entity, Base):
    __tablename__ = "upload_sessions"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    note_id: Mapped[uuid.UUID] = mapped_column()
    request_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_upload_request"),
        ForeignKeyConstraint(["owner_id", "file_id"], ["files.owner_id", "files.id"], name="fk_upload_file_owner"),
        ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_upload_note_owner"),
        CheckConstraint("status IN ('pending','received','completed','expired')", name="upload_status"),
    )


class NotePage(Entity, Base):
    __tablename__ = "note_pages"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[uuid.UUID] = mapped_column()
    file_id: Mapped[uuid.UUID] = mapped_column()
    position: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    pdf_page_number: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_page_note_owner"),
        ForeignKeyConstraint(["owner_id", "file_id"], ["files.owner_id", "files.id"], name="fk_page_file_owner"),
        UniqueConstraint("note_id", "file_id", name="uq_note_page_file"),
        UniqueConstraint("note_id", "position", name="uq_note_page_position"),
        UniqueConstraint("note_id", "pdf_page_number", name="uq_note_pdf_page_number"),
        CheckConstraint("pdf_page_number IS NULL OR pdf_page_number > 0", name="pdf_page_number_positive"),
        CheckConstraint("position >= 0 AND revision > 0", name="page_position_revision"),
    )


class FileVariant(Entity, Base):
    __tablename__ = "file_variants"
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    object_key: Mapped[str] = mapped_column(String(256), unique=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("file_id", "kind", name="uq_file_variant_kind"),
        CheckConstraint("kind IN ('reading','thumbnail')", name="file_variant_kind"),
        CheckConstraint("bytes > 0", name="file_variant_bytes"),
    )


class FileTicket(Entity, Base):
    __tablename__ = "file_tickets"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "file_id"], ["files.owner_id", "files.id"], name="fk_file_ticket_owner"),
    )


class PdfImport(Entity, Base):
    __tablename__ = "pdf_imports"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[uuid.UUID] = mapped_column()
    source_file_id: Mapped[uuid.UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    page_count: Mapped[int | None] = mapped_column(Integer)
    completed_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("note_id", name="uq_pdf_import_note"),
        UniqueConstraint("source_file_id", name="uq_pdf_import_source"),
        ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_pdf_note_owner"),
        ForeignKeyConstraint(["owner_id", "source_file_id"], ["files.owner_id", "files.id"], name="fk_pdf_source_owner"),
        CheckConstraint("completed_pages >= 0 AND (page_count IS NULL OR (page_count > 0 AND completed_pages <= page_count))", name="pdf_progress"),
        CheckConstraint("status IN ('uploading','queued','processing','ready','rejected')", name="pdf_status"),
    )
