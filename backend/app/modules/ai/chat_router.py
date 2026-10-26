import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.modules.ai import chat_service as service, image_context, image_schemas as image_s, pdf_context, source_context, text_context, schemas as s
from app.modules.ai.models import AiConversation, AiRun
from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes.models import Note
from app.shared.db import utcnow
from app.shared.dependencies import CurrentUser, DB
from app.shared.errors import AppError

router = APIRouter(tags=["ai-chat"])


@router.get("/ai/image-notebooks", response_model=list[image_s.ImageNotebookOut])
def image_notebooks(db: DB, user: CurrentUser):
    rows = db.execute(select(Note.id, Note.title, Note.revision, func.count(NotePage.id).label("page_count"))
                      .join(NotePage, NotePage.note_id == Note.id).join(File, File.id == NotePage.file_id)
                      .join(FileVariant, FileVariant.file_id == File.id).where(
                          Note.owner_id == user.id, Note.kind == "image", Note.deleted_at.is_(None),
                          NotePage.owner_id == user.id, NotePage.pdf_page_number.is_(None),
                          File.owner_id == user.id, File.status == "ready", FileVariant.kind == "reading")
                      .group_by(Note.id).order_by(Note.updated_at.desc()).limit(100))
    return [image_s.ImageNotebookOut(**dict(row._mapping)) for row in rows]


@router.get("/ai/image-notebooks/{note_id}", response_model=image_s.ImageNotebookDetail)
def image_notebook(note_id: UUID, db: DB, user: CurrentUser):
    return image_context.detail(db, user.id, note_id)


@router.post("/ai/image-preview", response_model=image_s.ImagePreviewOut)
def image_preview(data: image_s.ImageInput, db: DB, user: CurrentUser):
    return image_context.preview(db, user.id, data)


@router.get("/ai/pdf-notebooks", response_model=list[s.PdfNotebookOut])
def pdf_notebooks(db: DB, user: CurrentUser):
    records = db.execute(select(Note, PdfImport).join(PdfImport, PdfImport.note_id == Note.id).where(
        Note.owner_id == user.id, PdfImport.owner_id == user.id, Note.deleted_at.is_(None), PdfImport.status == "ready")
        .order_by(Note.updated_at.desc()).limit(100))
    return [s.PdfNotebookOut(id=note.id, title=note.title, revision=note.revision, page_count=item.page_count) for note, item in records]


@router.get("/ai/pdf-notebooks/{note_id}", response_model=s.PdfNotebookDetail)
def pdf_notebook(note_id: UUID, db: DB, user: CurrentUser):
    return pdf_context.detail(db, user.id, note_id)


@router.post("/ai/pdf-preview", response_model=s.PdfPreviewOut)
def preview(data: s.PdfInput, db: DB, user: CurrentUser):
    return pdf_context.preview(db, user.id, data)


@router.get("/ai/runs/{run_id}/source-pages/{page_id}", response_model=s.SourcePageOut)
def source_page(run_id: UUID, page_id: UUID, db: DB, user: CurrentUser):
    row = service.get_run(db, user.id, run_id)
    source = row.source
    if source is None or source_context.is_text(source):
        raise AppError(404, "source_not_found", "这次回答没有引用此页")
    note = source_context.validate(db, user.id, source) if source else None
    page = next((page for page in source["pages"] if page["page_id"] == str(page_id)), None) if source else None
    if not page:
        raise AppError(404, "source_not_found", "这次回答没有引用此页")
    return s.SourcePageOut(file_id=page["file_id"], title=source["title"], page_number=page["number"],
                           source_revision=source["revision"], source_changed=note.revision != source["revision"],
                           kind="image" if source_context.is_image(source) else "pdf", region=page.get("region"))


@router.get("/ai/runs/{run_id}/source-text", response_model=s.TextSourceReadOut)
def source_text(run_id: UUID, db: DB, user: CurrentUser):
    row = service.get_run(db, user.id, run_id)
    if not source_context.is_text(row.source):
        raise AppError(404, "source_not_found", "这次回答没有引用文字选段")
    return text_context.read(db, user.id, row.source)


@router.get("/ai/conversations", response_model=list[s.ConversationOut])
def list_conversations(db: DB, user: CurrentUser):
    return list(db.scalars(select(AiConversation).where(AiConversation.owner_id == user.id)
                           .order_by(AiConversation.created_at.desc()).limit(100)))


@router.post("/ai/conversations", response_model=s.ConversationOut, status_code=201)
def create_conversation(data: s.ConversationCreate, db: DB, user: CurrentUser):
    row = db.scalar(select(AiConversation).where(AiConversation.owner_id == user.id, AiConversation.request_key == data.request_key))
    if not row:
        row = AiConversation(owner_id=user.id, request_key=data.request_key)
        db.add(row)
        db.commit()
    return row


@router.get("/ai/conversations/{conversation_id}", response_model=s.ConversationDetail)
def detail(conversation_id: UUID, db: DB, user: CurrentUser):
    row = service.conversation(db, user.id, conversation_id)
    return s.ConversationDetail(**s.ConversationOut.model_validate(row).model_dump(),
                                runs=[s.RunOut.model_validate(r) for r in service.runs(db, user.id, row.id)])


@router.post("/ai/conversations/{conversation_id}/runs", response_model=s.RunOut, status_code=201)
def create_run(conversation_id: UUID, data: s.RunCreate, db: DB, user: CurrentUser):
    return service.create_run(db, user.id, conversation_id, data)


@router.get("/ai/runs/{run_id}", response_model=s.RunOut)
def run_detail(run_id: UUID, db: DB, user: CurrentUser):
    return service.get_run(db, user.id, run_id)


@router.post("/ai/runs/{run_id}/cancel", response_model=s.RunOut)
def cancel(run_id: UUID, db: DB, user: CurrentUser):
    # current_user already holds the owner lock; use the same order as worker saves.
    row = db.scalar(select(AiRun).where(AiRun.id == run_id, AiRun.owner_id == user.id).with_for_update())
    if not row:
        raise AppError(404, "run_not_found", "对话运行不存在")
    service.readable_source(row.source)
    if row.status in service.ACTIVE:
        row.status, row.finished_at, row.lease_until = "cancelled", utcnow(), None
        db.commit()
    return row


def event(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/ai/runs/{run_id}/events", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}}, "description": "Persisted meta/delta/state/done events; GET never invokes the model"}})
def events(run_id: UUID, request: Request, db: DB, user: CurrentUser):
    initial = s.RunOut.model_validate(service.get_run(db, user.id, run_id))
    owner, session_id = user.id, request.state.auth_session.id
    db.commit()  # No database connection or lock held for the lifetime of the stream.

    async def stream():
        current, offset, last_state = initial, len(initial.answer), None
        yield event("meta", initial.model_dump(mode="json"))
        while not await request.is_disconnected():
            try:
                current = await run_in_threadpool(service.snapshot, owner, run_id, session_id)
            except AppError as error:
                yield event("error", {"code": error.code, "message": error.message})
                return
            if len(current.answer) > offset:
                yield event("delta", {"offset": offset, "text": current.answer[offset:]})
                offset = len(current.answer)
            state = (current.status, current.usage, current.error_code, current.progress)
            if state != last_state:
                yield event("state", {"status": current.status, "usage": current.usage, "error_code": current.error_code,
                                      "progress": current.progress.model_dump(mode="json") if current.progress else None})
                last_state = state
            if current.status not in service.ACTIVE:
                yield event("done", current.model_dump(mode="json"))
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
