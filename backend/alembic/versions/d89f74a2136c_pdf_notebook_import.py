"""PDF import source and resumable page progress."""
from alembic import op
import sqlalchemy as sa

revision = "d89f74a2136c"
down_revision = "c2e41a9f3b67"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pdf_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("completed_pages", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_pdf_note_owner"),
        sa.ForeignKeyConstraint(["owner_id", "source_file_id"], ["files.owner_id", "files.id"], name="fk_pdf_source_owner"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", name="uq_pdf_import_note"),
        sa.UniqueConstraint("source_file_id", name="uq_pdf_import_source"),
        sa.CheckConstraint("completed_pages >= 0 AND (page_count IS NULL OR (page_count > 0 AND completed_pages <= page_count))", name="pdf_progress"),
        sa.CheckConstraint("status IN ('uploading','queued','processing','ready','rejected')", name="pdf_status"),
    )
    op.create_index("ix_pdf_imports_owner_id", "pdf_imports", ["owner_id"])


def downgrade():
    op.drop_table("pdf_imports")
