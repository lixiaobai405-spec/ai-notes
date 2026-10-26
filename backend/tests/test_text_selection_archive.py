"""Text provenance survives an offline archive and an empty-database restore."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4
import zipfile

import pytest
from sqlalchemy import select

from app.main import app
from app.modules.ai import chat_provider, chat_service, text_context
from app.modules.ai.models import AiRun, AiSavedNote
from app.modules.exports import service as exports
from app.modules.notes.models import Note, NoteVersion
from app.shared.config import settings
from app.shared.db import SessionLocal, get_db, utcnow
from app.shared.dependencies import current_user
from app.shared.errors import AppError
from app.shared.models import AuthSession
from conftest import login, mutate
from test_ai_chat import conversation, mock_http, read, submit
from test_archive_restore import _metadata, _restore, restore_database, restore_tool, target  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-text-archive-tests-not-real")
    settings.cache_clear()

    def forbidden_live_client():
        raise AssertionError("Live model requests are forbidden in archive tests")

    monkeypatch.setattr(chat_provider, "create_client", forbidden_live_client)
    yield
    settings.cache_clear()


def _document(value):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}]}


def _export(path, owner, session):
    with exports.create_archive(owner, session) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    return path


@pytest.fixture
def text_archive(client, monkeypatch, tmp_path):
    login(client)
    body = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "前文🙂所选e\u0301"},
             {"type": "inlineMath", "attrs": {"latex": "x^2"}}, {"type": "hardBreak"},
             {"type": "text", "text": "第二行"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [_document("列表")["content"][0]]}]},
        {"type": "blockMath", "attrs": {"latex": "E=mc^2"}},
        _document("不会发送的末尾正文")["content"][0],
    ]}
    response = mutate(client, "POST", "/notes", {"title": "旧版选段资料", "body_json": body,
                                               "plain_text": "不要信任客户端派生文字"})
    assert response.status_code == 201, response.text
    original = response.json()
    canonical = text_context.canonical_text(body)
    quote = "所选e\u0301x^2\n第二行\n列表\nE=mc^2"
    start = canonical.index(quote)
    selection = {"note_id": original["id"], "revision": original["revision"],
                 "start": start, "end": start + len(quote), "quote": quote}
    response = submit(client, conversation(client), "解释这段资料", text=selection)
    assert response.status_code == 201, response.text
    run = response.json()
    calls = mock_http(monkeypatch)
    assert chat_service.tick()
    assert read(client, run)["status"] == "succeeded"
    assert len(calls) == 1 and "不会发送的末尾正文" not in calls[0].content.decode("utf-8")
    response = mutate(client, "POST", f"/ai/runs/{run['id']}/save-note", {"title": "保留文字出处的回答"})
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["source"] == run["source"] and saved["cited_page_numbers"] == []
    response = mutate(client, "PATCH", "/notes/" + original["id"], {
        "revision": original["revision"], "title": "原笔记现已修改", "body_json": _document("全新正文"),
        "plain_text": "全新正文"})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        owner = db.get(Note, UUID(original["id"])).owner_id
        session = db.scalar(select(AuthSession).where(AuthSession.user_id == owner,
                                                     AuthSession.revoked_at.is_(None))).id
    path = _export(tmp_path / "text-selection.zip", owner, session)
    return SimpleNamespace(path=path, owner=owner, session=session, note=original, run=run, saved=saved,
                           body=body, canonical=canonical, source=run["source"])


def _restored_read(client, target, owner, run_id):
    def restored_db():
        with target.sessions() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = restored_db
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=owner)
    try:
        return client.get(f"/api/v1/ai/runs/{run_id}/source-text")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_api_text_run_save_edit_export_offline_verify_restore_reads_exact_old_version(
    client, text_archive, restore_tool, target,
):
    fixture = text_archive
    manifest, collections = _metadata(fixture.path)
    assert manifest["payloads"] == [next(item for item in manifest["payloads"] if item["path"] == "data.json")]
    assert not any(collections[name] for name in ("files", "file_variants", "note_pages", "pdf_imports"))
    for table in ("ai_runs", "ai_saved_notes"):
        assert collections[table][0]["source"] == fixture.source
        assert set(collections[table][0]["source"]) == {"kind", "note_id", "revision", "title", "start", "end", "quote"}
    # The distributed verifier must work in isolated Python without the backend
    # import path or dependencies and without any service access.
    checked = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "verify_export.py"), str(fixture.path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert checked.returncode == 0, checked.stderr
    result = _restore(restore_tool, fixture.path, target)
    assert result["objects"] == 0 and result["counts"] == manifest["counts"]
    with target.sessions() as db:
        run = db.get(AiRun, UUID(fixture.run["id"]))
        saved = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == run.id))
        assert run.source == saved.source == fixture.source and saved.cited_page_numbers == []
        assert run.input_messages == [] and run.lease_until is None
        assert db.get(Note, UUID(fixture.note["id"])).title == "原笔记现已修改"
        version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == UUID(fixture.note["id"]),
                                                     NoteVersion.revision == fixture.source["revision"]))
        assert version.snapshot["body_json"] == fixture.body
    response = _restored_read(client, target, fixture.owner, fixture.run["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"source": fixture.source, "text": fixture.canonical, "source_changed": True}


def test_recycled_text_source_can_export_restore_and_read_again_after_recovery(
    client, text_archive, restore_tool, target, tmp_path,
):
    fixture = text_archive
    with SessionLocal.begin() as db:
        db.get(Note, UUID(fixture.note["id"])).deleted_at = utcnow()
    path = _export(tmp_path / "recycled-text.zip", fixture.owner, fixture.session)
    assert restore_tool._load_verifier().verify_archive(path)["ai_saved_notes"] == 1
    _restore(restore_tool, path, target)
    assert _restored_read(client, target, fixture.owner, fixture.run["id"]).status_code == 404
    with target.sessions.begin() as db:
        note = db.get(Note, UUID(fixture.note["id"]))
        assert note.deleted_at is not None
        note.deleted_at = None
    response = _restored_read(client, target, fixture.owner, fixture.run["id"])
    assert response.status_code == 200 and response.json()["text"] == fixture.canonical


def _rewrite(path, output, change):
    with zipfile.ZipFile(path) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    data = json.loads(payloads["data.json"])
    change(data["collections"])
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payloads["data.json"] = raw
    manifest = json.loads(payloads["manifest.json"])
    manifest["counts"] = {name: len(rows) for name, rows in data["collections"].items()}
    for item in manifest["payloads"]:
        if item["path"] == "data.json":
            item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    payloads["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in payloads.items():
            archive.writestr(name, content)
    return output


@pytest.mark.parametrize("damage", ["quote", "utf16", "bool", "extra_pages", "missing_version", "duplicate_version",
                                    "version_owner", "note_kind", "snapshot_id", "snapshot_owner", "snapshot_kind", "snapshot_revision",
                                    "snapshot_title", "snapshot_body", "malformed_body", "citation"])
def test_offline_text_verifier_rejects_semantic_damage_with_repaired_checksums(
    text_archive, restore_tool, tmp_path, damage,
):
    def change(collections):
        source = collections["ai_runs"][0]["source"]
        version = next(row for row in collections["note_versions"]
                       if row["note_id"] == source["note_id"] and row["revision"] == source["revision"])
        snapshot = version["snapshot"]
        if damage == "quote": source["quote"] = "x" * len(source["quote"])
        elif damage == "utf16": source.update(start=source["start"] + 1, end=source["end"] + 1)
        elif damage == "bool": source["start"] = True
        elif damage == "extra_pages": source["pages"] = []
        elif damage == "missing_version": collections["note_versions"].remove(version)
        elif damage == "duplicate_version":
            duplicate = deepcopy(version); duplicate["id"] = str(uuid4())
            collections["note_versions"].append(duplicate)
        elif damage == "version_owner": version["owner_id"] = str(uuid4())
        elif damage == "note_kind": next(row for row in collections["notes"] if row["id"] == source["note_id"])["kind"] = "image"
        elif damage == "snapshot_id": snapshot["id"] = str(uuid4())
        elif damage == "snapshot_owner": snapshot["owner_id"] = str(uuid4())
        elif damage == "snapshot_kind": snapshot["kind"] = "image"
        elif damage == "snapshot_revision": snapshot["revision"] += 1
        elif damage == "snapshot_title": snapshot["title"] = "伪造标题"
        elif damage == "snapshot_body": snapshot["body_json"] = _document("伪造原文")
        elif damage == "malformed_body": snapshot["body_json"] = {"type": "doc", "content": [{"type": "text", "text": []}]}
        else: collections["ai_saved_notes"][0]["cited_page_numbers"] = [1]

    path = _rewrite(text_archive.path, tmp_path / (damage + ".zip"), change)
    verifier = restore_tool._load_verifier()
    with pytest.raises(verifier.ValidationError):
        verifier.verify_archive(path)


@pytest.mark.parametrize("damage", ["missing_version", "snapshot_owner", "snapshot_body", "extra_pages", "citation"])
def test_live_export_rejects_incomplete_text_provenance(text_archive, damage):
    fixture = text_archive
    with SessionLocal.begin() as db:
        version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == UUID(fixture.note["id"]),
                                                     NoteVersion.revision == fixture.source["revision"]))
        if damage == "missing_version": db.delete(version)
        elif damage in {"snapshot_owner", "snapshot_body"}:
            version.snapshot = {**version.snapshot, **({"owner_id": str(uuid4())} if damage == "snapshot_owner"
                                else {"body_json": _document("被破坏的历史正文")})}
        elif damage == "extra_pages":
            run = db.get(AiRun, UUID(fixture.run["id"]))
            run.source = {**run.source, "pages": []}
        else:
            db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == UUID(fixture.run["id"]))).cited_page_numbers = [1]
    with pytest.raises(AppError) as raised:
        exports.create_archive(fixture.owner, fixture.session)
    assert raised.value.code == "export_incomplete"


def test_offline_and_backend_canonical_rules_match(text_archive, restore_tool):
    verifier = restore_tool._load_verifier()
    assert verifier._canonical_text(text_archive.body) == text_context.canonical_text(text_archive.body)
    for body in ({}, {"type": "doc", "content": [{"content": []}]},
                 {"type": "doc", "content": [{"type": "inlineMath"}]},
                 {"type": "doc", "content": [None]},
                 {"type": "doc", "content": [{"type": "text"}]},
                 {"type": "doc", "content": [{"type": "hardBreak", "content": [None]}]}):
        try:
            expected = text_context.canonical_text(body)
        except ValueError:
            with pytest.raises(verifier.ValidationError):
                verifier._canonical_text(body)
        else:
            assert verifier._canonical_text(body) == expected
