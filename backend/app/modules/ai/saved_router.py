from uuid import UUID

from fastapi import APIRouter

from app.modules.ai import saved_notes as service, schemas as s
from app.shared.dependencies import CurrentUser, DB

router = APIRouter(tags=["ai-saved-notes"])


@router.get("/ai/conversations/{conversation_id}/saved-notes", response_model=list[s.SavedAnswerOut])
def saved_in_conversation(conversation_id: UUID, db: DB, user: CurrentUser):
    return service.list_saved(db, user.id, conversation_id)


@router.post("/ai/runs/{run_id}/save-note", response_model=s.SavedAnswerOut, status_code=201)
def save_answer(run_id: UUID, data: s.SaveAnswerIn, db: DB, user: CurrentUser):
    return service.save_answer(db, user, run_id, data)


@router.get("/notes/{note_id}/ai-answer", response_model=s.SavedAnswerProvenanceOut | None)
def source_of_note(note_id: UUID, db: DB, user: CurrentUser):
    return service.note_provenance(db, user, note_id)
