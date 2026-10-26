"""PDF single-region contracts, actual provider bytes and private provenance."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from uuid import UUID, uuid4

from PIL import Image
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from app.modules.ai import chat_service
from app.modules.ai.models import AiRun, AiSavedNote, AiStep
from app.modules.ai.schemas import PdfInput, PdfSourceOut
from app.modules.files import service as files
from app.modules.files.models import FileVariant, NotePage
from app.modules.notes.models import Note
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow

REGION = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}


def test_contract_single_region_and_legacy_pdf_sources():
    data = {"note_id": str(uuid4()), "revision": 1, "mode": "region", "page_numbers": [2], "region": REGION}
    assert PdfInput.model_validate(data).region.model_dump() == REGION
    for changes in ({"page_numbers": []}, {"page_numbers": [1, 2]}, {"region": None},
                    {"mode": "all", "page_numbers": []}, {"mode": "pages"},
                    {"region": {**REGION, "x": 0.9}}, {"region": {**REGION, "width": True}},
                    {"region": {**REGION, "height": float("nan")}}):
        with pytest.raises(ValidationError):
            PdfInput.model_validate({**data, **changes})
    source = {"note_id": data["note_id"], "revision": 1, "title": "PDF", "mode": "region",
              "pages": [{"page_id": str(uuid4()), "number": 2, "region": REGION}]}
    assert PdfSourceOut.model_validate(source).kind == "pdf"
    for changes in ({"pages": []}, {"pages": source["pages"] * 2},
                    {"mode": "all"}, {"mode": "pages"},
                    {"pages": [{**source["pages"][0], "region": None}]}):
        with pytest.raises(ValidationError):
            PdfSourceOut.model_validate({**source, **changes})
    legacy = {**source, "mode": "pages", "pages": [{"page_id": source["pages"][0]["page_id"], "number": 2}]}
    assert PdfSourceOut.model_validate(legacy).pages[0].region is None


@pytest.fixture
def fixture(client, monkeypatch, tmp_path):
    from conftest import login
    from test_pdf_chat import pdf_book, source
    from app.modules.ai import chat_provider
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-pdf-region-tests-not-real")
    settings.cache_clear()
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))
    monkeypatch.setattr(chat_provider, "create_client", lambda: (_ for _ in ()).throw(AssertionError("No live models")))
    login(client)
    note_id = pdf_book(client)
    with SessionLocal() as db:
        page, variant = db.execute(select(NotePage, FileVariant).join(
            FileVariant, FileVariant.file_id == NotePage.file_id).where(
                NotePage.note_id == UUID(note_id), NotePage.pdf_page_number == 2,
                FileVariant.kind == "reading")).one()
        image = Image.new("RGB", (100, 80), "red")
        image.paste("blue", (50, 0, 100, 80))
        output = io.BytesIO(); image.save(output, format="JPEG", quality=98)
        data = output.getvalue()
        files.storage().put(variant.object_key, data)
        variant.bytes = len(data)
        db.commit()
    selected = {**source(client, note_id, "pages", [2]), "mode": "region", "region": REGION}
    yield note_id, selected
    settings.cache_clear()


def test_region_preview_and_actual_mock_transport_receive_only_selected_crop(client, fixture, monkeypatch):
    from conftest import mutate
    from test_pdf_chat import mock, saved, submit
    note_id, selected = fixture
    calls = mock(monkeypatch)
    writes = []
    storage = files.storage()
    monkeypatch.setattr(storage, "put", lambda *args: writes.append(args))
    monkeypatch.setattr(files, "storage", lambda: storage)
    preview = mutate(client, "POST", "/ai/pdf-preview", selected)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert [value[key] for key in ("width", "height", "crop_width", "crop_height", "planned_calls")] == [100, 80, 50, 80, 1]
    assert not calls and not writes
    with SessionLocal() as db:
        assert not list(db.scalars(select(AiRun))) and not list(db.scalars(select(AiStep)))
    row = submit(client, note_id, pdf=selected).json()
    assert row["source"]["pages"][0]["region"] == REGION
    assert "object_key" not in json.dumps(row) and "file_id" not in json.dumps(row)
    assert chat_service.tick()
    assert saved(client, row)["status"] == "succeeded" and len(calls) == 1 and not writes
    content = calls[0]["messages"][-1]["content"]
    images = [base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in content if part["type"] == "image_url"]
    assert len(images) == 1
    with Image.open(io.BytesIO(images[0])) as crop:
        assert crop.size == (50, 80)
        red, green, blue = crop.getpixel((25, 40))
        assert blue > 220 and red < 25 and green < 25
    labels = " ".join(part["text"] for part in content if part["type"] == "text")
    assert "原PDF第2页" in labels and "选区" in labels
    assert "裁片外" in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("changes,code", [({"region": {**REGION, "width": 0.001}}, "pdf_region_too_small"),
    ({"page_numbers": [1, 2]}, "validation_error"), ({"region": {**REGION, "x": 0.75}}, "validation_error")])
def test_region_invalid_selection_never_queues_or_calls(client, fixture, changes, code):
    from conftest import mutate
    from test_pdf_chat import submit
    note_id, selected = fixture
    for response in (mutate(client, "POST", "/ai/pdf-preview", {**selected, **changes}),
                     submit(client, note_id, pdf={**selected, **changes})):
        assert response.status_code == 422, response.text
        if code != "validation_error":
            assert response.json()["code"] == code
    with SessionLocal() as db:
        assert not list(db.scalars(select(AiRun)))


def test_region_scope_saved_note_and_source_identity_survive_reordering(client, fixture, monkeypatch):
    from conftest import mutate
    from test_pdf_chat import mock, submit
    note_id, selected = fixture
    row = submit(client, note_id, pdf=selected).json()
    mock(monkeypatch); chat_service.tick()
    page = row["source"]["pages"][0]
    link = f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}"
    detail = client.get("/api/v1/ai/pdf-notebooks/" + note_id).json()
    assert all(item["file_id"] for item in detail["pages"])
    pages = client.get(f"/api/v1/notes/{note_id}/pages").json()
    reordered = mutate(client, "PATCH", f"/notes/{note_id}/pages/order", {
        "revision": detail["revision"], "page_ids": [item["id"] for item in reversed(pages)]})
    assert reordered.status_code == 200
    resolved = client.get(link).json()
    assert resolved["region"] == REGION and resolved["page_number"] == 2 and resolved["source_changed"]
    follow = mutate(client, "POST", f"/ai/conversations/{row['conversation_id']}/runs", {
        "request_key": str(uuid4()), "previous_run_id": row["id"], "prompt": "继续解释"})
    assert follow.status_code == 201, follow.text
    with SessionLocal() as db:
        assert "选区" in db.get(AiRun, UUID(follow.json()["id"])).input_messages[1]["content"]
    answer = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "PDF crop answer"})
    assert answer.status_code == 201, answer.text
    with SessionLocal.begin() as db:
        saved = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == UUID(row["id"])))
        note = db.get(Note, saved.note_id)
        assert saved.source["pages"][0]["region"] == REGION and "选区" in note.plain_text
        note.body_json = {"type": "doc", "content": []}; note.plain_text = ""
    provenance = client.get(f"/api/v1/notes/{answer.json()['note_id']}/ai-answer")
    assert provenance.status_code == 200 and provenance.json()["source"]["pages"][0]["region"] == REGION
    with SessionLocal.begin() as db:
        db.get(Note, UUID(note_id)).deleted_at = utcnow()
    assert client.get(link).status_code == 404


def test_legacy_pdf_hash_and_missing_kind_remain_retryable(client, fixture, monkeypatch):
    from test_ai_chat import conversation
    from test_pdf_chat import mock, saved, source, submit
    note_id, _ = fixture
    selected = source(client, note_id, "pages", [2])
    cid, key = conversation(client), str(uuid4())
    row = submit(client, note_id, conversation_id=cid, request_key=key, pdf=selected).json()
    # Exact historical pre-region/pre-image request payload, rather than the implementation's hash helper.
    payload = {"request_key": key, "previous_run_id": None, "model": "deepseek-flash",
               "prompt": "总结资料并引用原PDF页码", "pdf": selected}
    expected = hashlib.sha256(json.dumps({"conversation": cid, **payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(row["id"]))
        assert run.request_hash == expected
        run.request_hash = expected
        source_copy = deepcopy(run.source); source_copy.pop("kind", None); run.source = source_copy
    mock(monkeypatch); chat_service.tick()
    assert saved(client, row)["status"] == "succeeded"
    assert submit(client, note_id, conversation_id=cid, request_key=key, pdf=selected).json()["id"] == row["id"]


@pytest.mark.parametrize("damage", ["region", "multiple", "mode", "identity", "bytes"])
def test_worker_revalidates_region_metadata_and_bytes_before_model(client, fixture, monkeypatch, damage):
    from test_pdf_chat import mock, saved, submit
    note_id, selected = fixture
    row = submit(client, note_id, pdf=selected).json()
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(row["id"]))
        snapshot = deepcopy(run.source)
        if damage == "region": snapshot["pages"][0]["region"]["width"] = 0.001
        elif damage == "multiple": snapshot["pages"].append(deepcopy(snapshot["pages"][0]))
        elif damage == "mode": snapshot["mode"] = "all"
        elif damage == "identity": snapshot["pages"][0]["file_id"] = str(uuid4())
        else: files.storage().put(snapshot["pages"][0]["object_key"], b"corrupt")
        run.source = snapshot
    calls = mock(monkeypatch); chat_service.tick()
    assert not calls
    with SessionLocal() as db:
        terminal = db.get(AiRun, UUID(row["id"]))
        assert terminal.status == "failed" and terminal.error_code == "pdf_source_unavailable"


def test_region_privacy_preview_corruption_and_controlled_invalid_metadata(client, fixture, monkeypatch):
    from conftest import login, mutate
    from test_pdf_chat import mock, submit
    from app.shared.models import User
    from app.shared.security import hasher
    note_id, selected = fixture
    row = submit(client, note_id, pdf=selected).json()
    page = row["source"]["pages"][0]
    link = f"/api/v1/ai/runs/{row['id']}/source-pages/{page['page_id']}"
    with SessionLocal.begin() as db:
        db.add(User(username="region-outsider", display_name="Other", password_hash=hasher.hash("member-password")))
    login(client, "region-outsider", "member-password")
    assert mutate(client, "POST", "/ai/pdf-preview", selected).status_code == 404
    assert client.get(link).status_code == 404
    login(client)
    mutate(client, "POST", f"/ai/runs/{row['id']}/cancel", {})
    with SessionLocal.begin() as db:
        run = db.get(AiRun, UUID(row["id"]))
        snapshot = deepcopy(run.source)
        key = snapshot["pages"][0]["object_key"]
        snapshot["pages"][0]["region"] = {**REGION, "x": 0.99}
        run.source = snapshot
    assert client.get(link).status_code == 409
    assert client.get(f"/api/v1/ai/runs/{row['id']}").status_code == 409
    assert client.get(f"/api/v1/ai/conversations/{row['conversation_id']}").status_code == 409
    assert client.get(f"/api/v1/ai/runs/{row['id']}/events").status_code == 409
    files.storage().put(key, b"damaged")
    calls = mock(monkeypatch)
    assert mutate(client, "POST", "/ai/pdf-preview", selected).status_code == 409
    assert submit(client, note_id, pdf=selected).status_code == 409
    assert not calls


@pytest.mark.parametrize("source", [{}, [], ["damaged"], "damaged"])
def test_malformed_persisted_pdf_source_is_409_and_never_becomes_a_paid_text_run(client, fixture, monkeypatch, source):
    from test_pdf_chat import mock, submit
    note_id, selected = fixture
    row = submit(client, note_id, pdf=selected).json()
    with SessionLocal.begin() as db:
        db.get(AiRun, UUID(row["id"])).source = source
    assert client.get(f"/api/v1/ai/runs/{row['id']}").status_code == 409
    calls = mock(monkeypatch)
    chat_service.tick()
    assert not calls
    with SessionLocal() as db:
        assert db.get(AiRun, UUID(row["id"])).error_code == "pdf_source_unavailable"
