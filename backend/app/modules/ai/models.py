import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, utcnow
from app.shared.models import Entity


class AiCredential(Entity, Base):
    # Preserve the schema for already-applied migrations; shared credentials are not stored here.
    __tablename__ = "ai_credentials"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    key_suffix: Mapped[str] = mapped_column(String(4))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_test_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    test_status: Mapped[str | None] = mapped_column(String(16))
    test_code: Mapped[str | None] = mapped_column(String(32))
    available_models: Mapped[list[str]] = mapped_column(JSONB, default=list)

    __table_args__ = (
        # This index also covers owner-scoped listing and the owner foreign key.
        UniqueConstraint("owner_id", "provider", name="uq_ai_credentials_owner_provider"),
        CheckConstraint("revision > 0 AND key_version > 0", name="ai_credential_versions"),
        CheckConstraint("octet_length(ciphertext) > 28", name="ai_credential_ciphertext"),
        CheckConstraint("test_status IS NULL OR test_status IN ('valid','invalid','unavailable')", name="ai_credential_test_status"),
    )


class AiConversation(Entity, Base):
    __tablename__ = "ai_conversations"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(64), default="新对话")
    request_key: Mapped[uuid.UUID] = mapped_column()
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_ai_conversation_request"),
        UniqueConstraint("owner_id", "id", name="uq_ai_conversation_owner"),
    )


class AiRun(Entity, Base):
    # One row is one immutable user turn and its progressively persisted answer.
    __tablename__ = "ai_runs"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column()
    request_key: Mapped[uuid.UUID] = mapped_column()
    request_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    input_messages: Mapped[list] = mapped_column(JSONB)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "conversation_id"], ["ai_conversations.owner_id", "ai_conversations.id"], name="fk_ai_run_conversation_owner"),
        UniqueConstraint("owner_id", "id", name="uq_ai_run_owner"),
        UniqueConstraint("owner_id", "request_key", name="uq_ai_run_request"),
        CheckConstraint("status IN ('queued','running','succeeded','cancelled','interrupted','failed','truncated')", name="ai_run_status"),
        Index("ix_ai_runs_conversation", "owner_id", "conversation_id", "created_at"),
        Index("ix_ai_runs_claim", "status", "created_at"),
    )


class AiStep(Entity, Base):
    """Each paid PDF call is committed as running before the external POST."""
    __tablename__ = "ai_steps"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    pages: Mapped[list[int]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="running")
    answer: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_ai_step_position"),
        CheckConstraint("kind IN ('read','reduce','answer') AND status IN ('running','succeeded','failed')", name="ai_step_state"),
    )


class AiSavedNote(Entity, Base):
    """One editable note per completed answer, with independent immutable provenance."""
    __tablename__ = "ai_saved_notes"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    run_id: Mapped[uuid.UUID] = mapped_column()
    note_id: Mapped[uuid.UUID] = mapped_column()
    source: Mapped[dict | None] = mapped_column(JSONB)
    cited_page_numbers: Mapped[list[int]] = mapped_column(JSONB, default=list)
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "run_id"], ["ai_runs.owner_id", "ai_runs.id"], name="fk_saved_ai_run_owner"),
        ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_saved_ai_note_owner"),
        UniqueConstraint("owner_id", "run_id", name="uq_saved_ai_run"),
        UniqueConstraint("note_id", name="uq_saved_ai_note"),
    )
