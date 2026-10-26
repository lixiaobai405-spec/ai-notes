"""Create an editable note from a server-owned answer, without another model call."""
import re
from copy import deepcopy

from sqlalchemy import select

from app.modules.ai import chat_service, schemas as s
from app.modules.ai.models import AiRun, AiSavedNote
from app.modules.ai.answer_format import answer_document, plain_text
from app.modules.ai import source_context
from app.modules.notes import service as notes, schemas as ns
from app.modules.notes.models import Note
from app.shared.errors import AppError

CITATION = re.compile(r"\[\[p:([0-9]{1,3})\]\]")


def metadata(saved, note):
    return s.SavedAnswerOut(run_id=saved.run_id, note_id=note.id, note_title=note.title,
                            notebook_id=note.notebook_id, deleted_at=note.deleted_at,
                            saved_at=saved.created_at, source=chat_service.readable_source(saved.source),
                            cited_page_numbers=saved.cited_page_numbers)


def list_saved(db, owner, conversation_id):
    chat_service.conversation(db, owner, conversation_id)
    rows = db.execute(select(AiSavedNote, Note).join(Note, Note.id == AiSavedNote.note_id)
                      .join(AiRun, AiRun.id == AiSavedNote.run_id).where(
                          AiSavedNote.owner_id == owner, Note.owner_id == owner,
                          AiRun.owner_id == owner, AiRun.conversation_id == conversation_id)
                      .order_by(AiSavedNote.created_at, AiSavedNote.id))
    return [metadata(saved, note) for saved, note in rows]


def note_provenance(db, user, note_id):
    note = notes.owned(db, user, Note, note_id)
    row = db.execute(select(AiSavedNote, AiRun.model).join(AiRun, AiRun.id == AiSavedNote.run_id)
                     .where(AiSavedNote.note_id == note_id, AiSavedNote.owner_id == user.id,
                            AiRun.owner_id == user.id)).first()
    if not row:
        return None
    saved, model = row
    return s.SavedAnswerProvenanceOut(**metadata(saved, note).model_dump(), model=model)


def note_content(run, source):
    text_source = bool(source and source.kind == "text")
    allowed = {page.number for page in source.pages} if source and not text_source else set()
    image_source = bool(source and source.kind == "image")
    source_label = "图片第 " if image_source else "原 PDF 第 "
    cited = set()

    def readable(match):
        number = int(match[1])
        if number in allowed:
            cited.add(number)
            return f"（{source_label}{number} 页）"
        return match[0]

    def literal(text):
        return [{"type": "paragraph", **({"content": [{"type": "text", "text": line}]} if line else {})}
                for line in text.split("\n")]

    answer = answer_document(run.answer)
    if answer is not None:
        answer = deepcopy(answer)
        stack = [(answer, False)]
        while stack:
            node, protected = stack.pop()
            protected = protected or node["type"] in {"codeBlock", "inlineMath", "blockMath"} or any(
                mark["type"] in {"code", "link"} for mark in node.get("marks", []))
            if node["type"] == "text" and not protected:
                node["text"] = CITATION.sub(readable, node["text"])
            stack.extend((child, protected) for child in node.get("content", []))
    else:
        # Complex content stays complete and literal. Never guess citations
        # inside an unparsed code block or formula.
        answer = {"type": "doc", "content": literal(run.answer)}
    cited = sorted(cited)
    sections = [("问题", literal(run.prompt)), ("AI 回答", answer["content"]), ("生成模型", literal(run.model))]
    if text_source:
        reference = (f"{source.title}\n来源版本 {source.revision}\n"
                     f"原文码点范围 [{source.start}, {source.end})\n所选原文：\n{source.quote}")
        sections.append(("文字来源", literal(reference)))
    elif source:
        numbers = "、".join(str(page.number) for page in source.pages)
        reference = f"{source.title}\n{source_label}{numbers} 页\n来源版本 {source.revision}"
        if image_source or source.mode == "region":
            reference += "\n" + source_context.region_text(source.pages[0].region.model_dump() if source.pages[0].region else None)
        if cited:
            reference += "\n回答引用" + source_label + "、".join(map(str, cited)) + " 页"
        sections.append(("图片来源" if image_source else "PDF 来源", literal(reference)))
    content = []
    for title, nodes in sections:
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": title}]})
        content.extend(nodes)
    body = {"type": "doc", "content": content}
    return body, plain_text(body), cited


def save_answer(db, user, run_id, data):
    # current_user holds the owner's row lock, serializing saves and note mutations.
    run = chat_service.get_run(db, user.id, run_id)
    existing = db.scalar(select(AiSavedNote).where(AiSavedNote.owner_id == user.id, AiSavedNote.run_id == run_id))
    if existing:
        # Never replace a saved note or its edits, including when it is in the recycle bin.
        return metadata(existing, db.get(Note, existing.note_id))
    if run.status != "succeeded" or not run.answer.strip():
        raise AppError(409, "answer_not_complete", "请等待完整回答生成后再保存为笔记")
    source = source_context.public_source(run.source)
    body, plain, cited = note_content(run, source)
    try:
        create = ns.NoteCreate(title=data.title, notebook_id=data.notebook_id, body_json=body, plain_text=plain)
    except ValueError:
        raise AppError(422, "answer_note_too_large", "回答内容超过笔记正文限制，暂时无法保存") from None
    note = notes.new_note(db, user, create)
    saved = AiSavedNote(owner_id=user.id, run_id=run.id, note_id=note.id,
                        source=source.model_dump(mode="json") if source else None, cited_page_numbers=cited)
    db.add(saved)
    db.flush()
    result = metadata(saved, note)
    # The note, initial version, and provenance either all commit or all roll back.
    db.commit()
    return result
