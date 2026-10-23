"""Private image/region context, bounded real-adapter payloads, and durable provenance."""

import base64
import hashlib
import io
import json
from uuid import UUID, uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.modules.ai import chat_provider, chat_service
from app.modules.ai.models import AiRun, AiSavedNote, AiStep
from app.modules.files import service as files
from app.modules.files.models import File, FileVariant, NotePage
from app.modules.notes import service as notes
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import User
from app.shared.security import hasher
from conftest import login, mutate
from test_ai_chat import conversation, read, stream_bytes, submit as text_submit
from test_pdf_chat import pdf_book, source as pdf_source


REGION = {"x": 0.5, "y": 0, "width": 0.5, "height": 1}


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-image-chat-tests-never-a-real-key")
    settings.cache_clear()
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))

    def no_live():
        raise AssertionError("Image regression tests must never call a real model")

    monkeypatch.setattr(chat_provider, "create_client", no_live)
    yield
    settings.cache_clear()


def image_book(client, count=2):
    response = mutate(client, "POST", "/notes", {"title": "普通图片资料", "kind": "image"})
    assert response.status_code == 201, response.text
    nid = UUID(response.json()["id"])
    with SessionLocal.begin() as db:
        note = db.get(Note, nid)
        for number in range(1, count + 1):
            image = Image.new("RGB", (400, 200), "red")
            image.paste("blue", (200, 0, 400, 200))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=95)
            data = buffer.getvalue()
            fid = uuid4()
            key = f"{note.owner_id}/{fid}/reading.jpg"
            files.storage().put(key, data)
            db.add(File(id=fid, owner_id=note.owner_id, object_key=key + ".original",
                        filename=f"照片{number}.jpg", mime="image/jpeg", expected_bytes=len(data),
                        bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), status="ready",
                        # Original dimensions intentionally differ from the reading variant.
                        width=4000, height=2000))
            db.flush()
            db.add(FileVariant(file_id=fid, kind="reading", object_key=key, bytes=len(data)))
            db.add(NotePage(owner_id=note.owner_id, note_id=nid, file_id=fid, position=number - 1))
        db.flush()
        notes.revise(db, note, note.revision, {})
        notes.save_snapshot(db, note)
    return str(nid)


def detail(client, nid):
    response = client.get("/api/v1/ai/image-notebooks/" + nid)
    assert response.status_code == 200, response.text
    return response.json()


def image_source(client, nid, page=0, region=None):
    book = detail(client, nid)
    return {"note_id": nid, "revision": book["revision"], "page_id": book["pages"][page]["page_id"],
            "region": region}


def submit(client, nid, *, cid=None, image=None, **changes):
    cid = cid or conversation(client)
    return text_submit(client, cid, "解释图片里的内容并标注来源", image=image or image_source(client, nid), **changes)


def mock(monkeypatch, handler=None, answer="图片内容[[p:1]]"):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return handler(request) if handler else httpx.Response(200, content=stream_bytes(answer))

    monkeypatch.setattr(chat_provider, "create_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return calls


def image_parts(payload):
    return [part for message in payload["messages"] if isinstance(message["content"], list)
            for part in message["content"] if part["type"] == "image_url"]


def decoded_image(part):
    encoded = part["image_url"]["url"]
    assert encoded.startswith("data:image/jpeg;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1])))
    image.load()
    return image


def assert_blue(pixel):
    assert pixel[2] > 220 and pixel[0] < 30 and pixel[1] < 30


def assert_red(pixel):
    assert pixel[0] > 220 and pixel[1] < 30 and pixel[2] < 30


def assert_no_runs():
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AiRun)) == 0
        assert db.scalar(select(func.count()).select_from(AiStep)) == 0


@pytest.mark.parametrize("region,mode,size", [(None, "page", (400, 200)), (REGION, "region", (200, 200))])
def test_preview_reads_actual_variant_dimensions_and_never_queues_or_calls(client, region, mode, size):
    login(client)
    nid = image_book(client)
    book = detail(client, nid)
    assert [page["number"] for page in book["pages"]] == [1, 2]
    assert all(page["file_id"] and page["page_revision"] == 1 and page["bytes"] > 0 for page in book["pages"])
    response = mutate(client, "POST", "/ai/image-preview", image_source(client, nid, region=region))
    assert response.status_code == 200, response.text
    preview = response.json()
    assert (preview["width"], preview["height"]) == (400, 200)
    assert (preview["crop_width"], preview["crop_height"]) == size
    assert preview["planned_calls"] == 1
    assert preview["source"]["kind"] == "image" and preview["source"]["mode"] == mode
    assert preview["source"]["pages"][0]["region"] == region
    assert "object_key" not in json.dumps(preview) and "file_id" not in json.dumps(preview)
    assert_no_runs()


@pytest.mark.parametrize("region,mode,size", [(None, "page", (400, 200)), (REGION, "region", (200, 200))])
def test_whole_page_and_region_send_only_the_correct_pixels_once(client, monkeypatch, region, mode, size):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid, region=region)
    cid, key = conversation(client), str(uuid4())
    response = submit(client, nid, cid=cid, image=selected, request_key=key)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["source"]["kind"] == "image" and row["source"]["mode"] == mode
    assert row["source"]["pages"][0]["region"] == region
    assert "object_key" not in json.dumps(row) and "file_id" not in json.dumps(row)
    assert submit(client, nid, cid=cid, image=selected, request_key=key).json()["id"] == row["id"]
    changed = {**selected, "region": None if region else REGION}
    assert submit(client, nid, cid=cid, image=changed, request_key=key).status_code == 409
    calls = mock(monkeypatch)
    assert chat_service.tick() and not chat_service.tick()
    saved = read(client, row)
    assert saved["status"] == "succeeded" and len(calls) == 1
    parts = image_parts(calls[0])
    assert len(parts) == 1
    actual = decoded_image(parts[0])
    assert actual.size == size
    assert_blue(actual.getpixel((actual.width * 3 // 4, actual.height // 2)))
    if region is None:
        assert_red(actual.getpixel((actual.width // 4, actual.height // 2)))
    else:
        assert_blue(actual.getpixel((actual.width // 4, actual.height // 2)))
    assert submit(client, nid, cid=cid, image=selected, request_key=key).json()["id"] == row["id"]
    events = client.get(f"/api/v1/ai/runs/{row['id']}/events")
    assert "event: done" in events.text and "object_key" not in events.text
    assert not chat_service.tick() and len(calls) == 1


@pytest.mark.parametrize("region", [
    {**REGION, "x": -0.1}, {**REGION, "y": 1}, {**REGION, "width": 0},
    {**REGION, "height": -1}, {**REGION, "x": 0.8}, {**REGION, "height": 1.1},
    {**REGION, "x": True}, {**REGION, "width": "0.5"},
    {"x": 0, "y": 0, "width": 1}, {**REGION, "extra": "untrusted"},
    {"x": 0, "y": 0, "width": 0.0000001, "height": 0.0000001},
])
def test_invalid_or_subpixel_regions_fail_before_dispatch(client, region):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid, region=region)
    assert mutate(client, "POST", "/ai/image-preview", selected).status_code == 422
    assert submit(client, nid, image=selected).status_code == 422
    assert_no_runs()


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_coordinates_are_rejected_without_dispatch(client, token):
    login(client)
    nid = image_book(client)
    raw = json.dumps(image_source(client, nid, region={**REGION, "x": "NONFINITE"})).replace('"NONFINITE"', token)
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post("/api/v1/ai/image-preview", content=raw,
                           headers={"Origin": "http://testserver", "X-CSRF-Token": csrf,
                                    "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert_no_runs()


def test_foreign_page_stale_note_pdf_page_and_nonvision_model_are_rejected(client):
    login(client)
    nid, other = image_book(client), image_book(client)
    selected = image_source(client, nid)
    foreign_page = {**selected, "page_id": detail(client, other)["pages"][0]["page_id"]}
    assert mutate(client, "POST", "/ai/image-preview", foreign_page).status_code == 422
    assert submit(client, nid, image=foreign_page).status_code == 422
    stale = {**selected, "revision": selected["revision"] - 1}
    assert mutate(client, "POST", "/ai/image-preview", stale).status_code == 409
    assert submit(client, nid, image=stale).status_code == 409
    assert submit(client, nid, image=selected, model="deepseek-v4-pro").status_code == 422
    pdf_id = pdf_book(client)
    pdf_page = client.get(f"/api/v1/notes/{pdf_id}/pages").json()[0]
    pdf_note = client.get(f"/api/v1/notes/{pdf_id}").json()
    assert mutate(client, "POST", "/ai/image-preview", {
        "note_id": pdf_id, "revision": pdf_note["revision"], "page_id": pdf_page["id"], "region": None,
    }).status_code in {404, 409, 422}
    assert submit(client, nid, image=selected, pdf=pdf_source(client, pdf_id)).status_code == 422
    assert_no_runs()


@pytest.mark.parametrize("status", ["uploading", "processing", "rejected", "expired"])
def test_nonready_file_is_not_available_as_image_context(client, status):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid)
    with SessionLocal.begin() as db:
        page = db.get(NotePage, UUID(selected["page_id"]))
        db.get(File, page.file_id).status = status
    assert mutate(client, "POST", "/ai/image-preview", selected).status_code in {404, 409, 422}
    assert submit(client, nid, image=selected).status_code in {404, 409, 422}
    assert_no_runs()


@pytest.mark.parametrize("bad", ["missing", "bytes", "invalid-jpeg"])
def test_corrupt_or_missing_reading_variant_never_dispatches_model(client, monkeypatch, bad):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid)
    response = submit(client, nid, image=selected)
    assert response.status_code == 201, response.text
    row = response.json()
    with SessionLocal.begin() as db:
        page = db.get(NotePage, UUID(selected["page_id"]))
        variant = db.scalar(select(FileVariant).where(FileVariant.file_id == page.file_id, FileVariant.kind == "reading"))
        if bad == "missing":
            # Remove the DB mapping without relying on a storage-specific delete API.
            db.delete(variant)
        elif bad == "bytes":
            files.storage().put(variant.object_key, b"short")
        else:
            files.storage().put(variant.object_key, b"x" * variant.bytes)
    calls = mock(monkeypatch)
    assert chat_service.tick() and not chat_service.tick()
    assert read(client, row)["status"] in {"failed", "interrupted"}
    assert calls == []


@pytest.mark.parametrize("owner", ["admin", "alice"])
def test_private_sources_isolate_members_and_admin_in_both_directions(client, owner):
    login(client)
    with SessionLocal.begin() as db:
        db.add(User(username="alice", display_name="Alice", password_hash=hasher.hash("member-password")))
    if owner == "alice":
        login(client, "alice", "member-password")
    nid = image_book(client)
    selected = image_source(client, nid)
    response = submit(client, nid, image=selected)
    assert response.status_code == 201, response.text
    row = response.json()
    if owner == "admin":
        login(client, "alice", "member-password")
    else:
        login(client)
    assert nid not in {item["id"] for item in client.get("/api/v1/ai/image-notebooks").json()}
    assert client.get(f"/api/v1/ai/image-notebooks/{nid}").status_code == 404
    assert mutate(client, "POST", "/ai/image-preview", selected).status_code == 404
    assert submit(client, nid, image=selected).status_code == 404
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{selected['page_id']}").status_code == 404


def test_new_preview_paths_require_auth_csrf_and_origin(client):
    assert client.get("/api/v1/ai/image-notebooks").status_code == 401
    assert client.get(f"/api/v1/ai/image-notebooks/{uuid4()}").status_code == 401
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid)
    assert client.post("/api/v1/ai/image-preview", json=selected).status_code == 403
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert client.post("/api/v1/ai/image-preview", json=selected,
                       headers={"Origin": "https://untrusted.example", "X-CSRF-Token": csrf}).status_code == 403
    assert_no_runs()


def test_unknown_provider_outcome_is_single_attempt_and_replay_is_readonly(client, monkeypatch):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid, region=REGION)
    cid, key = conversation(client), str(uuid4())
    row = submit(client, nid, cid=cid, image=selected, request_key=key).json()

    def timeout(request):
        raise httpx.ReadTimeout("ambiguous provider result", request=request)

    calls = mock(monkeypatch, timeout)
    assert chat_service.tick() and not chat_service.tick()
    saved = read(client, row)
    assert saved["status"] in {"failed", "interrupted"} and saved["error_code"] == "provider_timeout"
    assert submit(client, nid, cid=cid, image=selected, request_key=key).json()["id"] == row["id"]
    assert client.get(f"/api/v1/ai/runs/{row['id']}/events").status_code == 200
    assert not chat_service.tick() and len(calls) == 1


def test_reorder_keeps_original_page_identity_number_and_rectangle(client, monkeypatch):
    login(client)
    nid = image_book(client)
    book = detail(client, nid)
    selected = image_source(client, nid, region=REGION)
    row = submit(client, nid, image=selected).json()
    calls = mock(monkeypatch)
    assert chat_service.tick()
    pages = [page["page_id"] for page in book["pages"]]
    moved = mutate(client, "PATCH", f"/notes/{nid}/pages/order", {"revision": book["revision"], "page_ids": pages[::-1]})
    assert moved.status_code == 200, moved.text
    now = detail(client, nid)
    assert now["pages"][1]["page_id"] == selected["page_id"]
    lookup = client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{selected['page_id']}")
    assert lookup.status_code == 200, lookup.text
    original = lookup.json()
    assert original["kind"] == "image" and original["page_number"] == 1 and original["region"] == REGION
    assert original["source_changed"] and original["file_id"] == book["pages"][0]["file_id"]
    ticket = mutate(client, "POST", f"/files/{original['file_id']}/ticket?kind=reading", {})
    assert ticket.status_code == 200, ticket.text
    assert client.get(ticket.json()["url"]).status_code == 200
    assert len(calls) == 1


def test_saved_region_source_survives_edits_replays_and_source_trash(client, monkeypatch):
    login(client)
    nid = image_book(client)
    selected = image_source(client, nid, region=REGION)
    row = submit(client, nid, image=selected).json()
    calls = mock(monkeypatch)
    assert chat_service.tick()
    saved = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "框选讲解"})
    assert saved.status_code == 201, saved.text
    value = saved.json()
    assert value["source"] == row["source"] and value["cited_page_numbers"] == [1]
    note_id = value["note_id"]
    note = client.get(f"/api/v1/notes/{note_id}").json()
    assert "图片" in note["plain_text"] and "普通图片资料" in note["plain_text"]
    edited = mutate(client, "PATCH", f"/notes/{note_id}", {"revision": note["revision"], "title": "整理后的讲解",
                                                          "plain_text": "自己的修改", "body_json": {}})
    assert edited.status_code == 200, edited.text
    replay = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "不要覆盖"})
    assert replay.status_code == 201 and replay.json()["note_id"] == note_id
    assert replay.json()["note_title"] == "整理后的讲解"
    assert client.get(f"/api/v1/notes/{note_id}").json()["plain_text"] == "自己的修改"
    provenance = client.get(f"/api/v1/notes/{note_id}/ai-answer").json()
    assert provenance["source"] == row["source"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AiSavedNote)) == 1
    original = client.get(f"/api/v1/notes/{nid}").json()
    assert mutate(client, "DELETE", f"/notes/{nid}", {"revision": original["revision"]}).status_code == 204
    assert client.get(f"/api/v1/ai/runs/{row['id']}/source-pages/{selected['page_id']}").status_code == 404
    assert client.get(f"/api/v1/notes/{note_id}/ai-answer").json()["source"] == row["source"]
    assert len(calls) == 1 and not chat_service.tick()


def test_source_trashed_before_execution_or_followup_prevents_further_calls(client, monkeypatch):
    login(client)
    nid = image_book(client)
    row = submit(client, nid).json()
    with SessionLocal.begin() as db:
        db.get(Note, UUID(nid)).deleted_at = utcnow()
    calls = mock(monkeypatch)
    assert chat_service.tick() and not chat_service.tick()
    assert read(client, row)["status"] in {"failed", "interrupted"} and calls == []
    other = image_book(client)
    completed = submit(client, other).json()
    assert chat_service.tick() and read(client, completed)["status"] == "succeeded"
    with SessionLocal.begin() as db:
        db.get(Note, UUID(other)).deleted_at = utcnow()
    assert text_submit(client, completed["conversation_id"], "继续解释", previous_run_id=completed["id"]).status_code == 404
    assert len(calls) == 1


def test_same_image_notebook_can_change_page_region_and_plain_followup_never_resends_history_images(client, monkeypatch):
    login(client)
    nid = image_book(client)
    cid = conversation(client)
    row = submit(client, nid, cid=cid, image=image_source(client, nid, region=REGION)).json()
    calls = mock(monkeypatch)
    assert chat_service.tick()
    second = submit(client, nid, cid=cid, image=image_source(client, nid, page=1), previous_run_id=row["id"])
    assert second.status_code == 201, second.text
    assert chat_service.tick()
    assert len(image_parts(calls[-1])) == 1
    assert all(isinstance(message["content"], str) for message in calls[-1]["messages"][:-1])
    follow = text_submit(client, cid, "解释刚才的结论", previous_run_id=second.json()["id"])
    assert follow.status_code == 201, follow.text
    assert chat_service.tick() and read(client, follow.json())["status"] == "succeeded"
    assert len(calls) == 3 and image_parts(calls[-1]) == []
    assert all(isinstance(message["content"], str) for message in calls[-1]["messages"])


@pytest.mark.parametrize("first_kind", ["image", "pdf"])
def test_conversation_scope_rejects_another_notebook_and_image_pdf_kind_switch(client, monkeypatch, first_kind):
    login(client)
    nid, other = image_book(client), image_book(client)
    pdf_id = pdf_book(client)
    cid = conversation(client)
    if first_kind == "image":
        row = submit(client, nid, cid=cid).json()
    else:
        row = text_submit(client, cid, "解释资料", pdf=pdf_source(client, pdf_id)).json()
    calls = mock(monkeypatch)
    assert chat_service.tick() and read(client, row)["status"] == "succeeded"
    if first_kind == "image":
        assert submit(client, other, cid=cid, previous_run_id=row["id"]).status_code == 422
        switched = text_submit(client, cid, "换成PDF", previous_run_id=row["id"], pdf=pdf_source(client, pdf_id))
    else:
        switched = submit(client, nid, cid=cid, previous_run_id=row["id"])
    assert switched.status_code == 422, switched.text
    assert len(calls) == 1 and not chat_service.tick()
