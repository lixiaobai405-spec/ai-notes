"""Frozen rich-text selections use codepoint offsets and never send the full note."""
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.modules.ai import chat_provider, chat_service, text_context
from app.modules.ai.models import AiRun, AiSavedNote, AiStep
from app.modules.ai.schemas import RunCreate
from app.modules.ai.text_schemas import TextInput, TextSourceOut
from app.modules.notes.models import Note, NoteVersion
from app.shared.config import settings
from app.shared.db import SessionLocal
from app.shared.models import User
from app.shared.security import hasher
from conftest import login, mutate
from test_ai_chat import conversation, mock_http, read, submit


@pytest.fixture(autouse=True)
def isolated_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-text-selection-tests-not-real")
    settings.cache_clear()
    monkeypatch.setattr(chat_provider, "create_client", lambda: (_ for _ in ()).throw(AssertionError("No live model")))
    yield
    settings.cache_clear()


def document(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def note(client, value="未选择的秘密🙂所选资料e\u0301后文秘密"):
    response = mutate(client, "POST", "/notes", {"title": "原文资料", "body_json": document(value),
                                               "plain_text": "客户端伪造的派生文字"})
    assert response.status_code == 201, response.text
    result = response.json()
    quote = "所选资料e\u0301" if "所选资料e\u0301" in value else value
    start = value.index(quote)
    return result, {"note_id": result["id"], "revision": result["revision"],
                    "start": start, "end": start + len(quote), "quote": quote}


def test_contract_strict_codepoint_range_and_mutually_exclusive_sources():
    selection = {"note_id": str(uuid4()), "revision": 1, "start": 2, "end": 4, "quote": "🙂中"}
    assert TextInput.model_validate(selection).quote == "🙂中"
    for changes in ({"revision": True}, {"revision": 0}, {"start": True}, {"start": -1},
                    {"end": "4"}, {"end": 2}, {"quote": "  "}, {"quote": "x\x00"},
                    {"quote": "\ud800"}, {"quote": "x" * 8001}, {"object_key": "private"}):
        with pytest.raises(ValidationError):
            TextInput.model_validate({**selection, **changes})
    source = {**selection, "kind": "text", "title": "资料"}
    assert TextSourceOut.model_validate(source).kind == "text"
    with pytest.raises(ValidationError):
        RunCreate.model_validate({"request_key": str(uuid4()), "prompt": "问题", "text": selection,
                                  "image": {"note_id": str(uuid4()), "revision": 1, "page_id": str(uuid4())}})


def test_canonical_text_preserves_unicode_lists_breaks_and_formulas():
    body = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "🙂e\u0301"},
             {"type": "inlineMath", "attrs": {"latex": "x^2"}}, {"type": "hardBreak"},
             {"type": "text", "text": "第二行"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [document("列表")["content"][0]]},
             {"type": "listItem", "content": [document("末项")["content"][0]]}]},
        {"type": "blockMath", "attrs": {"latex": "E=mc^2"}}]}
    assert text_context.canonical_text(body) == "🙂e\u0301x^2\n第二行\n列表\n末项\nE=mc^2"
    for broken in ({"type": "doc", "content": [None]}, {"type": "doc", "content": [{"type": "text", "text": 2}]},
                   {"type": "doc", "content": [{"type": "inlineMath", "attrs": {"latex": []}}]}):
        with pytest.raises(ValueError):
            text_context.canonical_text(broken)


def test_api_worker_pro_model_only_sends_quote_and_history_keeps_it(client, monkeypatch):
    login(client)
    original, selection = note(client)
    cid = conversation(client)
    response = submit(client, cid, "解释选段", text=selection, model="deepseek-v4-pro")
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["source"] == {**selection, "kind": "text", "title": original["title"]}
    assert row["progress"] is None
    calls = mock_http(monkeypatch)
    assert chat_service.tick()
    assert read(client, row)["status"] == "succeeded"
    payload = json.loads(calls[0].content)
    content = json.dumps(payload["messages"], ensure_ascii=False)
    assert selection["quote"] in content
    assert "未选择的秘密" not in content and "后文秘密" not in content and "客户端伪造" not in content
    with SessionLocal() as db:
        assert not list(db.scalars(select(AiStep)))
    followup = submit(client, cid, "继续解释", previous_run_id=row["id"])
    assert followup.status_code == 201, followup.text
    with SessionLocal() as db:
        messages = db.get(AiRun, UUID(followup.json()["id"])).input_messages
        assert selection["quote"] in messages[1]["content"]
        assert "未选择的秘密" not in json.dumps(messages, ensure_ascii=False)


def test_source_reader_keeps_original_version_after_edit_and_saved_answer(client, monkeypatch):
    login(client)
    original, selection = note(client)
    row = submit(client, conversation(client), text=selection).json()
    changed = mutate(client, "PATCH", "/notes/" + original["id"], {"revision": original["revision"],
                       "title": "已修改标题", "body_json": document("新的正文"), "plain_text": "新的正文"})
    assert changed.status_code == 200
    mock_http(monkeypatch)
    assert chat_service.tick()
    response = client.get(f"/api/v1/ai/runs/{row['id']}/source-text")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["source"] == row["source"] and detail["source_changed"] is True
    assert detail["text"] == "未选择的秘密🙂所选资料e\u0301后文秘密"
    assert detail["text"][selection["start"]:selection["end"]] == selection["quote"]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{uuid4()}").status_code == 404
    saved = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "整理回答"})
    assert saved.status_code in {200, 201}, saved.text
    assert saved.json()["source"] == row["source"] and saved.json()["cited_page_numbers"] == []
    saved_note = client.get("/api/v1/notes/" + saved.json()["note_id"]).json()
    assert "文字来源" in saved_note["plain_text"] and selection["quote"] in saved_note["plain_text"]
    with SessionLocal() as db:
        provenance = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == UUID(row["id"])))
        assert provenance.source == row["source"]


def test_resubmitted_successful_selection_keeps_frozen_version_after_original_edit(client, monkeypatch):
    login(client)
    original, selection = note(client)
    cid = conversation(client)
    first = submit(client, cid, text=selection).json()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(first["id"]))
        run.status = "succeeded"; run.answer = "首次回答"
    changed = mutate(client, "PATCH", "/notes/" + original["id"], {"revision": original["revision"],
                       "title": "改后的标题", "body_json": document("改后的正文不应发给模型"),
                       "plain_text": "改后的正文不应发给模型"})
    assert changed.status_code == 200
    response = submit(client, cid, "继续解释原选段", previous_run_id=first["id"], text=selection)
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["source"] == first["source"]
    assert second["source"]["revision"] == 1 and second["source"]["title"] == original["title"]
    calls = mock_http(monkeypatch)
    assert chat_service.tick()
    assert read(client, second)["status"] == "succeeded" and len(calls) == 1
    messages = json.loads(calls[0].content)["messages"]
    assert selection["quote"] in messages[-1]["content"] and selection["quote"] in messages[1]["content"]
    payload = json.dumps(messages, ensure_ascii=False)
    assert all(text not in payload for text in ("改后的正文", "改后的标题", "未选择的秘密", "后文秘密"))


@pytest.mark.parametrize("change", ["quote", "offset", "conversation", "deleted", "failed"])
def test_historical_selection_reuse_never_authorizes_different_or_unavailable_old_input(client, change):
    login(client)
    original, selection = note(client)
    cid = conversation(client)
    first = submit(client, cid, text=selection).json()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(first["id"]))
        run.status = "failed" if change == "failed" else "succeeded"
        run.answer = "已有回答"
    changed = mutate(client, "PATCH", "/notes/" + original["id"], {"revision": original["revision"],
                       "body_json": document("新正文"), "plain_text": "新正文"})
    assert changed.status_code == 200
    if change == "quote": selection["quote"] = "变" + selection["quote"][1:]
    if change == "offset": selection["start"] += 1; selection["end"] += 1
    if change == "deleted":
        assert mutate(client, "DELETE", "/notes/" + original["id"], {"revision": 2}).status_code == 204
    previous = first["id"]
    if change == "conversation": cid, previous = conversation(client), None
    response = submit(client, cid, previous_run_id=previous, text=selection)
    assert response.status_code in {404, 409}, response.text
    with SessionLocal() as db:
        assert len(list(db.scalars(select(AiRun)))) == 1


def test_historical_snapshot_owner_corruption_blocks_source_reader_and_worker(client):
    login(client)
    original, selection = note(client)
    row = submit(client, conversation(client), text=selection).json()
    with SessionLocal.begin() as db:
        version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == UUID(original["id"])))
        version.snapshot = {**version.snapshot, "owner_id": str(uuid4())}
    response = client.get(f"/api/v1/ai/runs/{row['id']}/source-text")
    assert response.status_code == 409 and response.json()["code"] == "text_source_unavailable"
    assert chat_service.tick()
    result = read(client, row)
    assert result["status"] == "failed" and result["error_code"] == "text_source_unavailable"


@pytest.mark.parametrize("damage", ["stale", "quote", "utf16", "outside", "missing_version", "invalid_version"])
def test_invalid_selection_never_queues_model_work(client, damage):
    login(client)
    original, selection = note(client)
    if damage == "stale": selection["revision"] += 1
    if damage == "quote": selection["quote"] = "伪造内容"
    if damage == "utf16": selection["start"] += 1; selection["end"] += 1
    if damage == "outside": selection["end"] = 99999
    if damage in {"missing_version", "invalid_version"}:
        with SessionLocal.begin() as db:
            version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == UUID(original["id"])))
            if damage == "missing_version": db.delete(version)
            else: version.snapshot = {**version.snapshot, "body_json": {"type": "doc", "content": [None]}}
    response = submit(client, conversation(client), text=selection)
    assert response.status_code in {409, 422}, response.text
    with SessionLocal() as db:
        assert not list(db.scalars(select(AiRun)))


def test_foreign_note_and_run_are_private_even_for_admin(client):
    with SessionLocal.begin() as db:
        db.add(User(username="text-owner", display_name="Owner", password_hash=hasher.hash("member-password")))
    login(client, "text-owner", "member-password")
    _, selection = note(client)
    row = submit(client, conversation(client), text=selection).json()
    login(client)
    assert submit(client, conversation(client), text=selection).status_code == 404
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-text").status_code == 404


@pytest.mark.parametrize("history_only", [False, True])
def test_deletion_before_worker_revalidates_current_and_historical_text(client, monkeypatch, history_only):
    login(client)
    original, selection = note(client)
    cid = conversation(client)
    row = submit(client, cid, text=selection).json()
    if history_only:
        with SessionLocal.begin() as db:
            first = db.get(AiRun, UUID(row["id"]))
            first.status = "succeeded"; first.answer = "已有回答"
        row = submit(client, cid, previous_run_id=row["id"]).json()
    assert mutate(client, "DELETE", "/notes/" + original["id"], {"revision": original["revision"]}).status_code == 204
    assert chat_service.tick()
    terminal = read(client, row)
    assert terminal["status"] == "failed" and terminal["error_code"] == "text_source_unavailable"


def test_deleted_source_answer_remains_readable_and_saveable_but_new_calls_stop(client):
    login(client)
    original, selection = note(client)
    cid = conversation(client)
    row = submit(client, cid, text=selection).json()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(row["id"])); run.status = "succeeded"; run.answer = "回答"
    assert mutate(client, "DELETE", "/notes/" + original["id"], {"revision": 1}).status_code == 204
    assert read(client, row)["answer"] == "回答"
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-text").status_code == 404
    assert submit(client, cid, previous_run_id=row["id"]).status_code in {404, 409}
    saved = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "保留回答"})
    assert saved.status_code in {200, 201}, saved.text
    restored = mutate(client, "POST", "/notes/" + original["id"] + "/restore", {"revision": 2})
    assert restored.status_code == 200, restored.text
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-text").status_code == 200


def test_text_scope_and_combined_utf8_budget_are_enforced(client):
    login(client)
    _, selection = note(client)
    cid = conversation(client)
    row = submit(client, cid, text=selection).json()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(row["id"])); run.status = "succeeded"; run.answer = "回答"
    _, other = note(client)
    assert submit(client, cid, previous_run_id=row["id"], text=other).status_code == 422
    huge = "中" * 8000
    _, large_selection = note(client, huge)
    response = submit(client, conversation(client), prompt="问" * 3000, text=large_selection)
    assert response.status_code == 422 and response.json()["code"] == "context_too_large"


def test_legacy_text_request_hash_does_not_change(client):
    login(client)
    cid, key = conversation(client), str(uuid4())
    row = submit(client, cid, request_key=key).json()
    payload = {"request_key": key, "previous_run_id": None, "model": "deepseek-flash", "prompt": "你好", "pdf": None}
    expected = hashlib.sha256(json.dumps({"conversation": cid, **payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with SessionLocal() as db:
        assert db.get(AiRun, UUID(row["id"])).request_hash == expected
    assert submit(client, cid, request_key=key).json()["id"] == row["id"]


@pytest.mark.parametrize("source", [{"kind": "text"}, {"kind": "unknown"}])
def test_damaged_source_is_controlled_and_never_becomes_plain_paid_run(client, source):
    login(client)
    _, selection = note(client)
    row = submit(client, conversation(client), text=selection).json()
    with SessionLocal.begin() as db:
        db.get(AiRun, UUID(row["id"])).source = deepcopy(source)
    assert client.get(f"/api/v1/ai/runs/{row['id']}").status_code == 409
    assert chat_service.tick()
    with SessionLocal() as db:
        result = db.get(AiRun, UUID(row["id"]))
        assert result.status == "failed" and result.error_code in {"text_source_unavailable", "pdf_source_unavailable"}
