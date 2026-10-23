"""Exercise later PDF/AI/export features with independent tab credentials."""

import io
import json
from uuid import uuid4
import zipfile

import pytest

from app.modules.ai import chat_provider, chat_service
from app.shared.config import settings
from app.worker import tick
from conftest import login
from test_ai_chat import mock_http, stream_bytes
from test_pdf_imports import pdf
from test_tab_sessions import create_members, headers, tab_login


@pytest.fixture
def tabs(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-tab-integration-never-a-real-key")
    settings.cache_clear()
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))

    def no_live():
        raise AssertionError("Tab integration tests must never call a real provider")

    monkeypatch.setattr(chat_provider, "create_client", no_live)
    create_members(client)
    sessions = {
        name: tab_login(client, name, "member-password" if name != "admin" else "admin-test-password")
        for name in ("alice", "bob", "admin")
    }
    # Every request below also carries a valid admin Cookie in the shared jar.
    login(client)
    assert client.get("/api/v1/auth/me").json()["username"] == "admin"
    yield sessions
    settings.cache_clear()


def create_pdf(client, owner):
    content = pdf()
    response = client.post("/api/v1/pdf-imports", headers=owner, json={
        "filename": "tab-private.pdf", "size": len(content), "request_key": str(uuid4()),
    })
    assert response.status_code == 201, response.text
    return response.json(), content


def finish_pdf(client, item, content, owner):
    response = client.put(item["upload"]["upload_url"], headers=owner | {
        "Content-Type": "application/pdf"}, content=content)
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/pdf-imports/{item['id']}/start", headers=owner, json={})
    assert response.status_code == 200, response.text
    assert tick()
    response = client.get(f"/api/v1/pdf-imports/{item['id']}", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["completed_pages"] == 3


def test_pdf_tab_upload_conversion_and_download_never_use_another_cookie_identity(client, tabs):
    owner = headers(tabs["alice"])
    item, content = create_pdf(client, owner)
    status_path = f"/api/v1/pdf-imports/{item['id']}"
    for name in ("bob", "admin"):
        other = headers(tabs[name])
        assert client.get(status_path, headers=other).status_code == 404
        assert client.put(item["upload"]["upload_url"], headers=other | {
            "Content-Type": "application/pdf"}, content=content).status_code == 404
        assert client.post(status_path + "/start", headers=other, json={}).status_code == 404
    finish_pdf(client, item, content, owner)
    pages = client.get(f"/api/v1/notes/{item['note_id']}/pages", headers=owner)
    assert pages.status_code == 200
    assert [page["position"] for page in pages.json()] == [0, 1, 2]
    file_id = item["upload"]["file"]["id"]
    ticket_path = f"/api/v1/files/{file_id}/ticket?kind=original"
    ticket = client.post(ticket_path, headers=owner, json={})
    assert ticket.status_code == 200, ticket.text
    url = ticket.json()["url"]
    download = client.get(url, headers=owner)
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-disposition"].startswith("attachment")
    for name in ("bob", "admin"):
        assert client.post(ticket_path, headers=headers(tabs[name]), json={}).status_code == 404
        assert client.get(url, headers=headers(tabs[name])).status_code == 404
    # A bad/revoked Bearer must not expose the authenticated shared admin Cookie.
    assert client.get(status_path, headers=owner | {"Authorization": "Bearer invalid"}).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=owner, json={}).status_code == 200
    assert client.get(status_path, headers=owner).status_code == 401
    assert client.get(url, headers=owner).status_code == 401
    assert client.get("/api/v1/auth/me").json()["username"] == "admin"
    assert client.get("/api/v1/auth/me", headers=headers(tabs["bob"])).status_code == 200


def test_pdf_chat_source_saved_answer_and_account_export_follow_the_tab_session(client, tabs, monkeypatch):
    owner = headers(tabs["alice"])
    item, content = create_pdf(client, owner)
    finish_pdf(client, item, content, owner)
    note_id = item["note_id"]
    detail = client.get(f"/api/v1/ai/pdf-notebooks/{note_id}", headers=owner)
    assert detail.status_code == 200, detail.text
    source = {"note_id": note_id, "revision": detail.json()["revision"], "mode": "pages", "page_numbers": [2]}
    preview = client.post("/api/v1/ai/pdf-preview", headers=owner, json=source)
    assert preview.status_code == 200 and preview.json()["batches"] == [[2]]
    response = client.post("/api/v1/ai/conversations", headers=owner, json={"request_key": str(uuid4())})
    assert response.status_code == 201, response.text
    conversation_id = response.json()["id"]
    run_path = f"/api/v1/ai/conversations/{conversation_id}/runs"
    body = {"request_key": str(uuid4()), "prompt": "解释第二页", "pdf": source}
    response = client.post(run_path, headers=owner, json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    assert client.post(run_path, headers=owner, json=body).json()["id"] == run["id"]
    calls = mock_http(monkeypatch, stream_bytes("第二页的回答[[p:2]]"))
    assert chat_service.tick() and not chat_service.tick()
    result_path = f"/api/v1/ai/runs/{run['id']}"
    result = client.get(result_path, headers=owner)
    assert result.status_code == 200 and result.json()["status"] == "succeeded"
    assert result.json()["answer"] == "第二页的回答[[p:2]]" and len(calls) == 1
    source_path = result_path + "/source-pages/" + run["source"]["pages"][0]["page_id"]
    assert client.get(source_path, headers=owner).json()["page_number"] == 2
    events = client.get(result_path + "/events", headers=owner)
    assert events.status_code == 200 and "event: done" in events.text
    saved = client.post(result_path + "/save-note", headers=owner, json={"title": "Alice 的回答"})
    assert saved.status_code == 201, saved.text
    saved_note = saved.json()
    assert saved_note["cited_page_numbers"] == [2]
    saved_path = f"/api/v1/notes/{saved_note['note_id']}"
    assert client.get(saved_path, headers=owner).json()["plain_text"].find("第二页的回答") >= 0
    assert client.get(saved_path + "/ai-answer", headers=owner).json()["run_id"] == run["id"]
    for name in ("bob", "admin"):
        other = headers(tabs[name])
        for path in (f"/api/v1/ai/pdf-notebooks/{note_id}", result_path, result_path + "/events",
                     source_path, saved_path, saved_path + "/ai-answer"):
            assert client.get(path, headers=other).status_code == 404
        assert client.post(run_path, headers=other, json=body).status_code == 404
        assert client.post(result_path + "/save-note", headers=other, json={"title": "不允许复制"}).status_code == 404

    # The archive must use the tab owner and tab session, including its later
    # session recheck, instead of switching to the shared Cookie's account.
    for name in ("alice", "bob", "admin"):
        response = client.post("/api/v1/exports/archive", headers=headers(tabs[name]), json={})
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            collections = json.loads(archive.read("data.json"))["collections"]
        expected_notes = {note_id, saved_note["note_id"]} if name == "alice" else set()
        assert {note["id"] for note in collections["notes"]} == expected_notes
        assert {row["id"] for row in collections["ai_runs"]} == ({run["id"]} if name == "alice" else set())
        assert all(row["owner_id"] == tabs[name]["user"]["id"] for row in collections["notes"])
        assert tabs[name]["session_token"] not in json.dumps(collections)
    assert len(calls) == 1
    assert client.post("/api/v1/auth/logout", headers=owner, json={}).status_code == 200
    for path in (result_path, result_path + "/events", source_path, saved_path):
        assert client.get(path, headers=owner).status_code == 401
    assert client.post("/api/v1/exports/archive", headers=owner, json={}).status_code == 401
    assert client.get("/api/v1/auth/me").json()["username"] == "admin"
    assert client.get(result_path, headers=headers(tabs["bob"])).status_code == 404
