"""Freeze a server-owned rich-text quote and locate its exact historical version."""
from sqlalchemy import select

from app.modules.ai.answer_format import plain_text
from app.modules.ai.text_schemas import TextSourceOut, TextSourceReadOut
from app.modules.notes.models import Note, NoteVersion
from app.modules.notes.schemas import validate_body, validate_pg_text
from app.shared.errors import AppError


def canonical_text(body):
    """Derive the editor's canonical text without trusting Note.plain_text.

    Offsets use Python Unicode codepoints, not JavaScript UTF-16 units. The
    archive verifier can use this helper without a database or live permissions.
    Malformed persisted documents raise ValueError rather than leaking a 500.
    """
    try:
        if not isinstance(body, dict):
            raise ValueError("invalid_rich_text")
        validate_body(body, "")
        stack = [body]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                raise ValueError("invalid_rich_text_node")
            kind = node.get("type")
            if kind is not None and not isinstance(kind, str):
                raise ValueError("invalid_rich_text_node")
            if kind == "text" and not isinstance(node.get("text"), str):
                raise ValueError("invalid_rich_text_text")
            if kind in {"inlineMath", "blockMath"}:
                attrs = node.get("attrs", {})
                if not isinstance(attrs, dict) or not isinstance(attrs.get("latex", ""), str):
                    raise ValueError("invalid_rich_text_formula")
            children = node.get("content", [])
            if not isinstance(children, list):
                raise ValueError("invalid_rich_text_content")
            stack.extend(children)
        result = plain_text(body)
        return validate_pg_text(result)
    except (TypeError, AttributeError, RecursionError, UnicodeError):
        raise ValueError("invalid_rich_text") from None


def _snapshot_text(source, snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get("id") != str(source.note_id)
            or snapshot.get("kind") != "text" or type(snapshot.get("revision")) is not int
            or snapshot["revision"] != source.revision or snapshot.get("title") != source.title):
        raise ValueError("text_snapshot_identity_changed")
    return canonical_text(snapshot.get("body_json"))


def validate_snapshot(source, snapshot):
    """Validate an immutable source against NoteVersion.snapshot; return full text.

    This is intentionally independent of current deletion state and database
    access so account export/restore can preserve recycled historical sources.
    Callers must separately authorize the note and historical version owner.
    """
    public = source if isinstance(source, TextSourceOut) else TextSourceOut.model_validate(source)
    text = _snapshot_text(public, snapshot)
    if public.end > len(text) or text[public.start:public.end] != public.quote:
        raise ValueError("text_selection_changed")
    return text


def _note(db, owner, note_id):
    note = db.scalar(select(Note).where(Note.id == note_id, Note.owner_id == owner,
                                      Note.kind == "text", Note.deleted_at.is_(None)))
    if not note:
        raise AppError(404, "text_not_found", "文字笔记不存在或无权访问")
    return note


def _version(db, owner, source):
    version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == source.note_id,
                         NoteVersion.owner_id == owner, NoteVersion.revision == source.revision))
    if not version:
        raise ValueError("text_version_missing")
    if not isinstance(version.snapshot, dict) or version.snapshot.get("owner_id") != str(owner):
        raise ValueError("text_snapshot_owner_changed")
    return version.snapshot


def freeze(db, owner, data):
    note = _note(db, owner, data.note_id)
    if note.revision != data.revision:
        raise AppError(409, "text_changed", "文字笔记已更新，请保存并重新选择原文")
    source = TextSourceOut(**data.model_dump(), kind="text", title=note.title)
    try:
        text = _snapshot_text(source, _version(db, owner, source))
    except (KeyError, TypeError, ValueError):
        raise AppError(409, "text_source_unavailable", "文字来源历史版本已失效，请重新加载原笔记") from None
    if source.end > len(text) or text[source.start:source.end] != source.quote:
        raise AppError(422, "text_selection_invalid", "所选文字与已保存原文不一致，请重新选择")
    return source.model_dump(mode="json")


def validate(db, owner, source):
    try:
        public = TextSourceOut.model_validate(source)
        note = _note(db, owner, public.note_id)
        validate_snapshot(public, _version(db, owner, public))
        return note
    except (KeyError, TypeError, ValueError):
        raise AppError(409, "text_source_unavailable", "本次引用的文字原版本已失效，停止继续调用模型") from None


def read(db, owner, source):
    note = validate(db, owner, source)
    public = TextSourceOut.model_validate(source)
    return TextSourceReadOut(source=public, text=validate_snapshot(public, _version(db, owner, public)),
                             source_changed=note.revision != public.revision)


def message(source):
    """Only the selected quote enters model context; the rest stays server-side."""
    return ("\n[文字来源：" + source["title"] + "；版本 " + str(source["revision"]) + "]\n"
            "<selected_text>\n" + source["quote"] + "\n</selected_text>")
