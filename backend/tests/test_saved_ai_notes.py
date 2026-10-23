"""Save persisted AI answers without invoking a provider or trusting model markup."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.main import app
from app.modules.ai import chat_provider
from app.modules.ai.models import AiRun, AiSavedNote
from app.modules.notes import service as notes
from app.modules.notes.models import Note, NoteVersion
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.errors import AppError
from app.shared.models import User
from app.shared.security import hasher
from conftest import login, mutate
from test_ai_chat import conversation, submit
from test_pdf_chat import pdf_book, source


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-save-answer-tests-never-a-real-key")
    settings.cache_clear()
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))

    def no_live(*_args, **_kwargs):
        raise AssertionError("Saving an answer must never call the real model")

    monkeypatch.setattr(chat_provider, "create_client", no_live)
    monkeypatch.setattr(chat_provider, "stream_chat", no_live)
    yield
    settings.cache_clear()


def completed(client, prompt="解释矩阵", answer="矩阵由按行、列排列的数构成。", **kwargs):
    cid = conversation(client)
    response = submit(client, cid, prompt, **kwargs)
    assert response.status_code == 201, response.text
    row = response.json()
    with SessionLocal.begin() as db:
        item = db.get(AiRun, UUID(row["id"]))
        item.answer, item.status, item.finished_at = answer, "succeeded", utcnow()
    return cid, row


def save(client, row, **changes):
    return mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "矩阵学习笔记", **changes})


def notebook(client, name="学习资料"):
    response = mutate(client, "POST", "/notebooks", {"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def member(name):
    with SessionLocal.begin() as db:
        db.add(User(username=name, display_name=name, password_hash=hasher.hash("member-password")))


def counts():
    with SessionLocal() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Note, NoteVersion, AiSavedNote))


def document_text(document):
    stack, found = [document], []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "text":
                found.append(node["text"])
            stack.extend(reversed(node.get("content", [])))
    return "\n".join(found)


def test_first_save_is_normal_editable_note_with_initial_version_and_provenance(client):
    login(client)
    book = notebook(client)
    cid, row = completed(client)
    response = save(client, row, title="  矩阵学习笔记  ", notebook_id=book)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["run_id"] == row["id"] and saved["note_title"] == "矩阵学习笔记"
    assert saved["notebook_id"] == book and saved["deleted_at"] is None
    assert saved["source"] is None and saved["cited_page_numbers"] == [] and saved["saved_at"]
    assert counts() == (1, 1, 1)
    detail = client.get(f"/api/v1/notes/{saved['note_id']}").json()
    assert detail["kind"] == "text" and detail["revision"] == 1
    assert detail["notebook_id"] == book
    for text in ["解释矩阵", "矩阵由按行、列排列的数构成。"]:
        assert text in detail["plain_text"] and text in document_text(detail["body_json"])
    versions = client.get(f"/api/v1/notes/{saved['note_id']}/versions").json()
    assert len(versions) == 1 and versions[0]["revision"] == 1
    assert versions[0]["snapshot"]["body_json"] == detail["body_json"]
    provenance = client.get(f"/api/v1/notes/{saved['note_id']}/ai-answer").json()
    assert provenance == {**saved, "model": "deepseek-flash"}
    assert client.get(f"/api/v1/ai/conversations/{cid}/saved-notes").json() == [saved]
    edited = mutate(client, "PATCH", f"/notes/{saved['note_id']}", {
        "revision": 1, "title": "手工整理后的笔记", "plain_text": "自己的补充",
        "body_json": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "自己的补充"}]}]},
    })
    assert edited.status_code == 200 and edited.json()["revision"] == 2


def test_replay_after_edit_returns_existing_metadata_without_overwriting_content(client):
    login(client)
    cid, row = completed(client)
    first = save(client, row).json()
    book = notebook(client)
    edited = mutate(client, "PATCH", f"/notes/{first['note_id']}", {
        "revision": 1, "title": "用户改过的标题", "notebook_id": book,
        "plain_text": "保留我的编辑", "body_json": {},
    })
    assert edited.status_code == 200, edited.text
    replay = save(client, row, title="不要覆盖", notebook_id=None)
    assert replay.status_code == 201
    assert replay.json()["note_id"] == first["note_id"]
    assert replay.json()["note_title"] == "用户改过的标题" and replay.json()["notebook_id"] == book
    assert replay.json()["saved_at"] == first["saved_at"]
    detail = client.get(f"/api/v1/notes/{first['note_id']}").json()
    assert detail["plain_text"] == "保留我的编辑" and detail["revision"] == 2
    assert counts() == (1, 2, 1)
    assert client.get(f"/api/v1/ai/conversations/{cid}/saved-notes").json() == [replay.json()]


def test_deleted_saved_note_is_not_silently_recreated_on_replay(client):
    login(client)
    cid, row = completed(client)
    first = save(client, row).json()
    assert mutate(client, "DELETE", f"/notes/{first['note_id']}", {"revision": 1}).status_code == 204
    replay = save(client, row, title="不能产生副本")
    assert replay.status_code == 201 and replay.json()["note_id"] == first["note_id"]
    assert replay.json()["deleted_at"] is not None
    assert counts() == (1, 2, 1)
    assert client.get(f"/api/v1/notes/{first['note_id']}/ai-answer").status_code == 404
    assert client.get(f"/api/v1/ai/conversations/{cid}/saved-notes").json() == [replay.json()]


def test_concurrent_double_click_with_two_sessions_creates_one_note_and_version(client):
    login(client)
    _, row = completed(client)
    barrier = Barrier(2)
    with TestClient(app) as other:
        login(other)
        first_csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        second_csrf = other.get("/api/v1/auth/csrf").json()["csrf_token"]

        def click(target, token):
            barrier.wait(timeout=10)
            return target.post(f"/api/v1/ai/runs/{row['id']}/save-note", json={"title": "双击保存"},
                               headers={"Origin": "http://testserver", "X-CSRF-Token": token})

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(click, client, first_csrf), pool.submit(click, other, second_csrf)
            responses = [a.result(timeout=30), b.result(timeout=30)]
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["note_id"] == responses[1].json()["note_id"]
    assert counts() == (1, 1, 1)


@pytest.mark.parametrize("status,answer", [
    ("queued", ""), ("running", "部分"), ("interrupted", "部分"),
    ("failed", ""), ("cancelled", "部分"), ("truncated", "达到上限的回答"),
    ("succeeded", ""), ("succeeded", " \n\t "),
])
def test_only_completed_nonempty_answers_can_be_saved(client, status, answer):
    login(client)
    _, row = completed(client)
    with SessionLocal.begin() as db:
        item = db.get(AiRun, UUID(row["id"]))
        item.status, item.answer = status, answer
    response = save(client, row)
    assert response.status_code == 409 and response.json()["code"] == "answer_not_complete"
    assert counts() == (0, 0, 0)


@pytest.mark.parametrize("payload", [
    {"title": ""}, {"title": "  \t "}, {"title": "x" * 201},
    {"title": "ab\x00cd"}, {"title": "ab\ud800cd"}, {"title": None},
    {"title": "正常", "api_key": "must-not-be-accepted"},
])
def test_save_input_rejects_invalid_database_text_and_extra_fields(client, payload):
    login(client)
    _, row = completed(client)
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    # ASCII JSON intentionally transports the invalid surrogate to validation,
    # instead of failing early in the HTTP client's UTF-8 encoder.
    response = client.post(f"/api/v1/ai/runs/{row['id']}/save-note",
                           content=json.dumps(payload, ensure_ascii=True).encode("ascii"),
                           headers={"Content-Type": "application/json", "Origin": "http://testserver", "X-CSRF-Token": csrf})
    assert response.status_code == 422, response.text
    assert counts() == (0, 0, 0)


def test_notebook_ownership_and_failed_creation_leave_no_partial_note(client):
    member("alice")
    login(client, "alice", "member-password")
    foreign_book = notebook(client)
    login(client)
    _, row = completed(client)
    response = save(client, row, notebook_id=foreign_book)
    assert response.status_code == 404
    assert counts() == (0, 0, 0)
    assert save(client, row).status_code == 201


def test_snapshot_failure_rolls_back_note_version_and_saved_relation(client, monkeypatch):
    login(client)
    _, row = completed(client)

    def unavailable(*_args, **_kwargs):
        raise AppError(503, "snapshot_unavailable", "模拟暂时不可用")

    with monkeypatch.context() as patch:
        patch.setattr(notes, "save_snapshot", unavailable)
        assert save(client, row).status_code == 503
    assert counts() == (0, 0, 0)
    assert save(client, row).status_code == 201
    assert counts() == (1, 1, 1)


def test_answer_exceeding_note_structure_limit_is_rejected_without_partial_rows(client):
    login(client)
    _, row = completed(client, answer="a\n\n" * 10_000 + "end")
    response = save(client, row)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "answer_note_too_large"
    assert counts() == (0, 0, 0)
    with SessionLocal() as db:
        run = db.get(AiRun, UUID(row["id"]))
        assert run.status == "succeeded" and run.answer.endswith("end")


def test_auth_csrf_origin_and_member_admin_owner_isolation(client):
    assert client.get("/api/v1/ai/conversations/00000000-0000-0000-0000-000000000001/saved-notes").status_code == 401
    assert client.get("/api/v1/notes/00000000-0000-0000-0000-000000000001/ai-answer").status_code == 401
    member("alice")
    login(client, "alice", "member-password")
    cid, row = completed(client)
    path = f"/api/v1/ai/runs/{row['id']}/save-note"
    assert client.post(path, json={"title": "试验"}, headers={"Origin": "http://testserver"}).status_code == 403
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert client.post(path, json={"title": "试验"}, headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}).status_code == 403
    assert counts() == (0, 0, 0)
    saved = save(client, row).json()
    member("bob")
    for username, password in [("admin", "admin-test-password"), ("bob", "member-password")]:
        login(client, username, password)
        assert client.get(f"/api/v1/ai/conversations/{cid}/saved-notes").status_code == 404
        assert client.get(f"/api/v1/notes/{saved['note_id']}/ai-answer").status_code == 404
        assert save(client, row).status_code == 404
    assert counts() == (1, 1, 1)
    mutate(client, "POST", "/auth/logout", {})
    assert save(client, row).status_code == 401


def test_ordinary_owned_live_note_has_no_ai_provenance(client):
    login(client)
    ordinary = mutate(client, "POST", "/notes", {"title": "手写笔记"}).json()
    response = client.get(f"/api/v1/notes/{ordinary['id']}/ai-answer")
    assert response.status_code == 200 and response.json() is None
    assert client.get(f"/api/v1/ai/conversations/{conversation(client)}/saved-notes").json() == []


def test_pdf_snapshot_citations_are_safe_and_source_deletion_preserves_answer_without_page_access(client):
    login(client)
    nid = pdf_book(client)
    selected = source(client, nid, "pages", [3, 1])
    answer = "第一处[[p:3]]，第二处[[p:1]]，再次[[p:3]]；未选页[[p:2]]、不存在[[p:99]]、负数[[p:-1]]。"
    cid, row = completed(client, answer=answer, pdf=selected)
    frozen = row["source"]
    # A previously completed answer remains saveable when its original PDF has
    # been removed. Its retained page references grant no access to that PDF.
    source_note = client.get(f"/api/v1/notes/{nid}").json()
    assert mutate(client, "DELETE", f"/notes/{nid}", {"revision": source_note["revision"]}).status_code == 204
    saved = save(client, row).json()
    assert saved["source"] == frozen and saved["cited_page_numbers"] == [1, 3]
    detail = client.get(f"/api/v1/notes/{saved['note_id']}").json()
    assert frozen["title"] in detail["plain_text"]
    assert "[[p:1]]" not in detail["plain_text"] and "[[p:3]]" not in detail["plain_text"]
    assert "1" in detail["plain_text"] and "3" in detail["plain_text"]
    provenance = client.get(f"/api/v1/notes/{saved['note_id']}/ai-answer").json()
    assert provenance["source"] == frozen and provenance["cited_page_numbers"] == [1, 3]
    assert client.get(f"/api/v1/ai/conversations/{cid}/saved-notes").json() == [saved]
    for page in frozen["pages"]:
        assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}").status_code == 404
    exposed = json.dumps([saved, detail, provenance], ensure_ascii=False)
    assert "object_key" not in exposed and "file_id" not in exposed
    with SessionLocal() as db:
        relation = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == UUID(row["id"])))
        assert "object_key" not in json.dumps(relation.source) and "file_id" not in json.dumps(relation.source)


def test_hostile_model_markup_is_safe_while_supported_markdown_keeps_format(client):
    login(client)
    question = '<img src=x onerror="alert(1)"> 怎么理解？'
    answer = '<script>alert(document.cookie)</script>\n[点击](javascript:alert(2))\n**粗体**\n```html\n<iframe src="https://evil.example"></iframe>\n```'
    _, row = completed(client, prompt=question, answer=answer)
    saved = save(client, row).json()
    detail = client.get(f"/api/v1/notes/{saved['note_id']}").json()
    assert question in detail["plain_text"] and "粗体" in detail["plain_text"]
    flattened = document_text(detail["body_json"])
    assert question in flattened and "<script>alert(document.cookie)</script>" in flattened
    stack = [detail["body_json"]]
    while stack:
        node = stack.pop()
        assert node["type"] in {"doc", "heading", "paragraph", "text", "hardBreak", "codeBlock"}
        assert all(mark["type"] == "bold" for mark in node.get("marks", []))
        assert set(node.get("attrs", {})) <= {"level", "language"}
        stack.extend(node.get("content", []))


def test_markdown_math_and_pdf_citations_share_chat_and_editable_saved_content(client):
    login(client)
    nid = pdf_book(client)
    selected = source(client, nid, "pages", [1, 3])
    answer = ("# 能量推导\n\n**结论**：$E=mc^2$，来源[[p:1]]。\n\n"
              "3. 第一项\n4. 第二项\n\n> 核对依据\n\n"
              "```python\nprint('[[p:3]]')\n```\n\n"
              "`[[p:3]]` 与 [链接中的[[p:3]]](https://example.com/ref)\n\n"
              "$$\n\\text{[[p:3]]}\n$$")
    answer += "\n\n$" + "x" * 8193 + "[[p:3]]$"
    _, row = completed(client, answer=answer, pdf=selected)
    assert row["formatted_answer"] is None  # The initial queued response is plain.
    fetched = client.get(f"/api/v1/ai/runs/{row['id']}").json()
    assert fetched["answer"] == answer and fetched["formatted_answer"]["fallback"] is False
    chat_body = fetched["formatted_answer"]["body_json"]
    saved = save(client, row).json()
    assert saved["cited_page_numbers"] == [1]
    detail = client.get(f"/api/v1/notes/{saved['note_id']}").json()
    nodes, stack = [], [detail["body_json"]]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node.get("content", []))
    assert any(n["type"] == "heading" and n.get("attrs", {}).get("level") == 1 for n in nodes)
    assert any(n["type"] == "orderedList" and n["attrs"]["start"] == 3 for n in nodes)
    assert any(n["type"] == "inlineMath" and n["attrs"]["latex"] == "E=mc^2" for n in nodes)
    assert any(n["type"] == "blockMath" and "[[p:3]]" in n["attrs"]["latex"] for n in nodes)
    assert any(n["type"] == "codeBlock" and n["attrs"]["language"] == "python" for n in nodes)
    assert any(n.get("text", "").endswith("[[p:3]]$") and n.get("marks") == [{"type": "code"}] for n in nodes)
    assert "原 PDF 第 1 页" in detail["plain_text"] and "[[p:3]]" in detail["plain_text"]
    assert "[[p:1]]" in document_text(chat_body) and "[[p:1]]" not in document_text(detail["body_json"])
    stream = client.get(f"/api/v1/ai/runs/{row['id']}/events").text
    done = next(block for block in stream.split("\n\n") if block.startswith("event: done"))
    assert json.loads(done.split("data: ", 1)[1])["formatted_answer"] == fetched["formatted_answer"]


def test_complex_answer_falls_back_without_losing_original_text_or_interpreting_citations(client):
    login(client)
    answer = "a\n\n" * 10_000 + "[[p:1]]"
    _, row = completed(client, answer=answer)
    result = client.get(f"/api/v1/ai/runs/{row['id']}").json()
    assert result["answer"] == answer
    assert result["formatted_answer"] == {"body_json": None, "fallback": True}
    assert counts() == (0, 0, 0)
