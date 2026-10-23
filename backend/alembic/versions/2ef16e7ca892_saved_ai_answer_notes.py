"""One editable note per AI answer, retaining independent source provenance."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2ef16e7ca892"
down_revision = "63b9d8343e02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_ai_run_owner", "ai_runs", ["owner_id", "id"])
    op.create_table(
        "ai_saved_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("source", postgresql.JSONB(), nullable=True),
        sa.Column("cited_page_numbers", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["owner_id", "run_id"], ["ai_runs.owner_id", "ai_runs.id"], name="fk_saved_ai_run_owner"),
        sa.ForeignKeyConstraint(["owner_id", "note_id"], ["notes.owner_id", "notes.id"], name="fk_saved_ai_note_owner"),
        sa.UniqueConstraint("owner_id", "run_id", name="uq_saved_ai_run"),
        sa.UniqueConstraint("note_id", name="uq_saved_ai_note"),
    )


def downgrade():
    op.drop_table("ai_saved_notes")
    op.drop_constraint("uq_ai_run_owner", "ai_runs", type_="unique")
