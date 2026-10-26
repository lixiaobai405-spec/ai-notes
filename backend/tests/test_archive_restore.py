"""Account archive recovery uses a separate, disposable restore-only database.

The ordinary pytest database remains the source of synthetic export fixtures.
Only the uniquely named database created below may be truncated or dropped.
"""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import uuid
import zipfile

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from PIL import Image

from app.modules.ai import image_context, pdf_context
from app.modules.ai.models import AiRun, AiSavedNote, AiStep
from app.modules.exports import service as exports
from app.modules.files import pdf_service, service as files
from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes import service as notes
from app.modules.notes.models import Folder, Note, NoteVersion
from app.shared.config import settings
from app.shared.db import Base, SessionLocal, engine
from app.shared.models import AuthSession, Job, User
from app.shared.security import verify_password
from test_exports import account as export_account
from test_pdf_region import fixture as pdf_region_book


ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "restore-test-only-password"
TABLES = tuple(model.__tablename__ for model in exports.FIELDS)


def test_pdf_region_api_save_export_verify_restore_retains_provenance(
    client, pdf_region_book, restore_tool, target, monkeypatch, tmp_path,
):
    from conftest import mutate
    from test_pdf_chat import mock, submit
    note_id, selected = pdf_region_book
    row = submit(client, note_id, pdf=selected).json()
    calls = mock(monkeypatch)
    from app.modules.ai import chat_service
    assert chat_service.tick()
    saved = mutate(client, "POST", f"/ai/runs/{row['id']}/save-note", {"title": "PDF region retained"})
    assert saved.status_code == 201, saved.text
    saved_id = uuid.UUID(saved.json()["note_id"])
    # pdf_book is deliberately light. Complete only this export fixture's
    # original/thumbnail objects so the production archive checks all assets.
    storage = files.storage()
    with SessionLocal.begin() as db:
        owner = db.get(Note, uuid.UUID(note_id)).owner_id
        auth = db.scalar(select(AuthSession).where(AuthSession.user_id == owner, AuthSession.revoked_at.is_(None)))
        session_id = auth.id
        for file in db.scalars(select(File).where(File.owner_id == owner)):
            if file.mime == "application/pdf":
                data = b"%PDF-synthetic-region"
            else:
                reading = db.scalar(select(FileVariant).where(FileVariant.file_id == file.id, FileVariant.kind == "reading"))
                data = storage.read(reading.object_key)
                key = reading.object_key + ".thumbnail"
                storage.put(key, data)
                db.add(FileVariant(file_id=file.id, kind="thumbnail", object_key=key, bytes=len(data)))
            storage.put(file.object_key, data)
            file.bytes = file.expected_bytes = len(data)
            file.sha256 = hashlib.sha256(data).hexdigest()
    path = tmp_path / "saved-api-pdf-region.zip"
    with exports.create_archive(owner, session_id) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    verifier = restore_tool._load_verifier()
    assert verifier.verify_archive(path)["ai_saved_notes"] == 1
    _restore(restore_tool, path, target)
    monkeypatch.setattr(settings(), "storage_root", str(target.storage_root))
    with target.sessions() as db:
        run = db.get(AiRun, uuid.UUID(row["id"]))
        provenance = db.scalar(select(AiSavedNote).where(AiSavedNote.note_id == saved_id))
        assert provenance.source == saved.json()["source"]
        assert provenance.source["pages"][0]["region"] == selected["region"]
        assert "选区" in db.get(Note, saved_id).plain_text
        assert pdf_context.validate(db, owner, run.source)
        crop = pdf_context.read_region(run.source)[0]
        with Image.open(io.BytesIO(crop)) as image:
            assert image.size == (50, 80)
    assert len(calls) == 1


def test_restore_pdf_region_roundtrip_and_offline_verification(
    restore_tool, export_account, target, tmp_path, monkeypatch,
):
    """Add a separate PDF-region case without changing the legacy fixture."""
    region = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
    with SessionLocal.begin() as db:
        run = db.get(AiRun, export_account.runs[0])
        source = json.loads(json.dumps(run.source))
        source["mode"] = "region"; source["pages"][0]["region"] = region
        run.source = source
        saved = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == run.id))
        saved.source = {**saved.source, "mode": "region", "pages": [{**saved.source["pages"][0], "region": region}]}
    path = tmp_path / "pdf-region.zip"
    with exports.create_archive(export_account.owner, export_account.session) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    _restore(restore_tool, path, target)
    monkeypatch.setattr(settings(), "storage_root", str(target.storage_root))
    with target.sessions() as db:
        run = db.get(AiRun, export_account.runs[0])
        saved = db.scalar(select(AiSavedNote).where(AiSavedNote.run_id == run.id))
        assert run.source["mode"] == saved.source["mode"] == "region"
        assert run.source["pages"][0]["region"] == saved.source["pages"][0]["region"] == region
        assert "object_key" not in json.dumps(saved.source)
        assert pdf_context.validate(db, export_account.owner, run.source)


@pytest.mark.parametrize("damage", ["missing", "multiple", "mode", "outside", "bool", "nan"])
def test_offline_verifier_rejects_conflicting_pdf_region(restore_tool, archive_file, tmp_path, damage):
    """Repair hashes so failures concern source semantics rather than ZIP damage."""
    with zipfile.ZipFile(archive_file) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    data = json.loads(payloads["data.json"])
    source = next(row["source"] for row in data["collections"]["ai_runs"] if row["source"]["kind"] == "pdf")
    source["mode"] = "region"
    source["pages"][0]["region"] = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
    if damage == "missing": source["pages"][0].pop("region")
    elif damage == "multiple": source["pages"].append(dict(source["pages"][0]))
    elif damage == "mode": source["mode"] = "all"
    elif damage == "outside": source["pages"][0]["region"]["width"] = 0.9
    elif damage == "bool": source["pages"][0]["region"]["height"] = True
    else: source["pages"][0]["region"]["height"] = float("nan")
    raw = json.dumps(data, ensure_ascii=False).encode()
    payloads["data.json"] = raw
    manifest = json.loads(payloads["manifest.json"])
    for entry in manifest["payloads"]:
        if entry["path"] == "data.json": entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    payloads["manifest.json"] = json.dumps(manifest).encode()
    path = tmp_path / (damage + ".zip")
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in payloads.items(): archive.writestr(name, raw)
    verifier = restore_tool._load_verifier()
    with pytest.raises(verifier.ValidationError):
        verifier.verify_archive(path)


@pytest.fixture(scope="module")
def restore_tool():
    path = ROOT / "scripts" / "restore_export.py"
    spec = importlib.util.spec_from_file_location("archive_restore_test_tool", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _owned_database_name(value):
    assert re.fullmatch(r"notes_restore_test_[0-9a-f]{32}", value)
    return value


@pytest.fixture(scope="module")
def restore_database(tmp_path_factory):
    name = _owned_database_name("notes_restore_test_" + uuid.uuid4().hex)
    url = engine.url.set(database=name)
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    target = None
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{_owned_database_name(name)}"'))
    try:
        storage_root = tmp_path_factory.mktemp("restore-migration-objects")
        environment = {
            **os.environ,
            "DATABASE_URL": url.render_as_string(hide_password=False),
            "DEEPSEEK_API_KEY": "",
            "STORAGE_BACKEND": "local",
            "STORAGE_ROOT": str(storage_root),
            "APP_ENV": "development",
        }
        migration = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "backend", env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
        assert migration.returncode == 0, migration.stderr
        target = create_engine(url)
        yield SimpleNamespace(name=name, url=url.render_as_string(hide_password=False), engine=target)
    finally:
        if target is not None:
            target.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{_owned_database_name(name)}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def target(restore_database, tmp_path):
    # The explicit guard is repeated at every destructive statement.
    assert restore_database.engine.url.database == _owned_database_name(restore_database.name)
    names = ",".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    with restore_database.engine.begin() as connection:
        connection.execute(text("TRUNCATE " + names + " CASCADE"))
    storage_root = tmp_path / "restored-objects"
    storage_root.mkdir()
    return SimpleNamespace(**vars(restore_database), storage_root=storage_root,
                           sessions=sessionmaker(bind=restore_database.engine, expire_on_commit=False))


@pytest.fixture
def archive_file(export_account, tmp_path):
    path = tmp_path / "account-export.zip"
    with exports.create_archive(export_account.owner, export_account.session) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    return path


def _restore(tool, archive, target, **overrides):
    arguments = {"database_url": target.url, "storage_root": target.storage_root, "password": PASSWORD}
    arguments.update(overrides)
    return tool.restore_archive(archive, **arguments)


def _counts(target):
    with target.sessions() as db:
        return {table.name: db.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables}


def _object_files(root):
    return [path for path in root.rglob("*") if path.is_file()]


def _metadata(path):
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("manifest.json")), json.loads(archive.read("data.json"))["collections"]


def test_restore_roundtrip_preserves_all_collections_assets_and_live_readers(
    restore_tool, archive_file, export_account, target, monkeypatch,
):
    manifest, source = _metadata(archive_file)
    result = _restore(restore_tool, archive_file, target)
    assert result == {"owner_id": str(export_account.owner), "counts": manifest["counts"],
                      "normalized_steps": 0, "synthesized_jobs": 0, "objects": 7}
    counts = _counts(target)
    assert len(manifest["counts"]) == 14
    assert {name: counts[name] for name in TABLES} == manifest["counts"]
    assert counts["users"] == 1 and counts["upload_sessions"] == 1
    assert not any(counts[name] for name in ("sessions", "ai_credentials", "password_resets",
                                           "invite_codes", "file_tickets", "jobs", "outbox_events"))
    monkeypatch.setattr(settings(), "storage_root", str(target.storage_root))
    with target.sessions.begin() as db:
        owner = db.get(User, export_account.owner)
        assert (owner.username, owner.display_name, owner.role, owner.status) == ("admin", "管理员", "user", "active")
        assert verify_password(owner.password_hash, PASSWORD)
        assert not verify_password(owner.password_hash, "admin-test-password")
        text_note = db.get(Note, export_account.text)
        output = notes.note_output(db, text_note)
        assert output.title == "保存的 PDF 回答" and output.tag_ids
        assert output.body_json == next(row for row in source["notes"] if row["id"] == str(text_note.id))["body_json"]
        historical = db.scalar(select(NoteVersion).where(NoteVersion.note_id == text_note.id))
        assert historical.snapshot == source["note_versions"][0]["snapshot"]
        assert db.get(Note, export_account.trash).deleted_at is not None
        assert {row.id for row in db.scalars(select(NotePage))} == set(export_account.pages)
        assert {row.id for row in db.scalars(select(FileVariant))} == set(export_account.variants)
        assert {row.id for row in db.scalars(select(AiSavedNote))} == {
            uuid.UUID(row["id"]) for row in source["ai_saved_notes"]}
        pdf_import = db.scalar(select(PdfImport))
        import_output = pdf_service.output(db, pdf_import)
        assert import_output.status == "ready" and import_output.completed_pages == 1
        assert import_output.upload.status == "completed"
        for run_id in export_account.runs:
            run = db.get(AiRun, run_id)
            assert run.input_messages == [] and run.lease_until is None
            original = next(row for row in source["ai_runs"] if row["id"] == str(run_id))
            assert run.answer == original["answer"] and run.usage == original["usage"]
            page = run.source["pages"][0]
            assert "reading_path" not in page and page["object_key"]
            assert page["page_id"] == original["source"]["pages"][0]["page_id"]
            validator = image_context if run.source["kind"] == "image" else pdf_context
            validator.validate(db, owner.id, run.source)
            if run.source["kind"] == "image":
                assert page["region"] == {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
            ticket = files.create_ticket(db, owner, uuid.UUID(page["file_id"]), "reading")
            raw_token = ticket["url"].split("ticket=", 1)[1].split("&", 1)[0]
            reading, mime = files.read_ticket(db, owner, uuid.UUID(page["file_id"]), raw_token, "reading")
            assert mime == "image/jpeg" and len(reading) == page["bytes"]
        with zipfile.ZipFile(archive_file) as archive:
            for row in source["files"]:
                restored = db.get(File, uuid.UUID(row["id"]))
                assert restored.status == row["status"]
                if row["original_path"]:
                    raw = files.storage().read(restored.object_key)
                    assert raw == archive.read(row["original_path"])
                    assert hashlib.sha256(raw).hexdigest() == row["sha256"]
            for row in source["file_variants"]:
                restored = db.get(FileVariant, uuid.UUID(row["id"]))
                assert files.storage().read(restored.object_key) == archive.read(row["path"])
    assert len(_object_files(target.storage_root)) == 7


def test_restore_topological_folders_and_dangling_historical_ids_remain_exact(
    restore_tool, export_account, target, tmp_path,
):
    parent_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    child_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    with SessionLocal.begin() as db:
        db.add(Folder(id=parent_id, owner_id=export_account.owner, name="根目录"))
        db.flush()
        db.add(Folder(id=child_id, owner_id=export_account.owner, name="子目录", parent_id=parent_id))
        version = db.scalar(select(NoteVersion).where(NoteVersion.note_id == export_account.text))
        version.snapshot = {**version.snapshot, "notebook_id": str(uuid.uuid4()), "tag_ids": [str(uuid.uuid4())]}
        expected = version.snapshot
    path = tmp_path / "folder-history.zip"
    with exports.create_archive(export_account.owner, export_account.session) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    _restore(restore_tool, path, target)
    with target.sessions() as db:
        assert db.get(Folder, child_id).parent_id == parent_id
        assert db.scalar(select(NoteVersion).where(NoteVersion.note_id == export_account.text)).snapshot == expected


def test_restore_normalizes_running_step_and_synthesizes_terminal_failed_jobs(
    restore_tool, export_account, target, tmp_path, monkeypatch,
):
    payload = b"failed-image-original"
    file_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        failed = File(id=file_id, owner_id=export_account.owner, object_key=f"{export_account.owner}/{file_id}/original",
                      filename="待重试.png", mime="image/png", expected_bytes=len(payload), bytes=len(payload),
                      sha256=hashlib.sha256(payload).hexdigest(), status="processing")
        db.add(failed)
        db.flush()
        db.add(Job(owner_id=export_account.owner, kind="image.process", dedupe_key=f"image_{file_id}",
                   payload={"file_id": str(file_id)}, status="failed", error_code="synthetic_processing_error",
                   attempts=3, max_attempts=3))
        image_page = db.get(NotePage, export_account.pages[1])
        db.add(NotePage(owner_id=export_account.owner, note_id=image_page.note_id, file_id=file_id, position=1))
        step = db.scalar(select(AiStep).where(AiStep.run_id == export_account.runs[0]))
        step.status, step.answer = "running", "部分已保存回答"
        pdf = db.scalar(select(PdfImport))
        pdf.status, pdf.error_code = "processing", "synthetic_pdf_error"
        db.add(Job(owner_id=export_account.owner, kind="pdf.import", dedupe_key=f"pdf_{pdf.id}",
                   payload={"import_id": str(pdf.id)}, status="failed", error_code="synthetic_pdf_error",
                   attempts=3, max_attempts=3))
        pdf_id = pdf.id
    export_account.store.put(f"{export_account.owner}/{file_id}/original", payload)
    path = tmp_path / "terminal-failures.zip"
    with exports.create_archive(export_account.owner, export_account.session) as stream, path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    result = _restore(restore_tool, path, target)
    assert result["normalized_steps"] == 1 and result["synthesized_jobs"] == 2
    monkeypatch.setattr(settings(), "storage_root", str(target.storage_root))
    with target.sessions() as db:
        step = db.scalar(select(AiStep).where(AiStep.run_id == export_account.runs[0]))
        assert step.status == "failed" and step.answer == "部分已保存回答" and step.error_code
        jobs = db.scalars(select(Job)).all()
        assert len(jobs) == 2 and all(job.status == "failed" for job in jobs)
        assert all(job.worker_token is None and job.lease_until is None for job in jobs)
        assert files.file_output(db, db.get(File, file_id)).status == "failed"
        assert files.file_output(db, db.get(File, file_id)).error_code == "synthetic_processing_error"
        output = pdf_service.output(db, db.get(PdfImport, pdf_id))
        assert output.status == "failed" and output.error_code == "synthetic_pdf_error"
        assert output.upload.status == "completed"


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_test",
    "postgresql+psycopg://notes:notes@example.invalid:55432/notes_restore_test",
    "sqlite:///notes_restore_test.db",
    "postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_restore_test?service=production",
    "postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_restore_test?hostaddr=192.0.2.1",
    "postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_restore_test?options=-csearch_path%3Dother",
    "postgresql+psycopg://127.0.0.1:55432/notes_restore_test",
    "postgresql+psycopg://notes:notes@127.0.0.1/notes_restore_test",
    None,
])
def test_restore_refuses_unsafe_targets_before_connecting(restore_tool, archive_file, tmp_path, monkeypatch, url):
    def no_connection(*args, **kwargs):
        pytest.fail("An invalid restore target must never create a database engine")
    monkeypatch.setattr(restore_tool, "create_engine", no_connection)
    with pytest.raises(restore_tool.RestoreError) as raised:
        restore_tool.restore_archive(archive_file, database_url=url, storage_root=tmp_path / "empty", password=PASSWORD)
    assert raised.value.code == "restore_target_invalid"


def test_restore_refuses_nonempty_database_without_touching_objects(restore_tool, archive_file, target):
    with target.sessions.begin() as db:
        db.add(User(username="existing", display_name="原数据", password_hash="leave-this-hash-alone"))
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target)
    assert raised.value.code == "restore_database_not_empty"
    assert _counts(target)["users"] == 1 and not _object_files(target.storage_root)


def test_restore_refuses_nonempty_storage_without_touching_existing_files(restore_tool, archive_file, target):
    existing = target.storage_root / "keep.bin"
    existing.write_bytes(b"leave-existing-data-alone")
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target)
    assert raised.value.code == "restore_storage_not_empty"
    assert existing.read_bytes() == b"leave-existing-data-alone"
    assert not any(_counts(target).values())


def test_restore_refuses_symlink_storage_ancestor(restore_tool, archive_file, target, tmp_path):
    real = tmp_path / "actual-storage-parent"
    real.mkdir()
    alias = tmp_path / "storage-alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks unavailable on this test host: {error.errno}")
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target, storage_root=alias / "objects")
    assert raised.value.code == "restore_storage_invalid"
    assert not list(real.iterdir()) and not any(_counts(target).values())


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junctions are platform-specific")
def test_restore_refuses_windows_junction_storage_ancestor(restore_tool, archive_file, target, tmp_path):
    real = tmp_path / "junction-real"
    real.mkdir()
    keep = real / "keep.bin"
    keep.write_bytes(b"junction target must not be removed")
    alias = tmp_path / "junction-alias"
    assert alias.parent.resolve() == real.parent.resolve() == tmp_path.resolve()
    creation = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(real)],
        capture_output=True, text=True, errors="replace", timeout=10,
    )
    if creation.returncode != 0:
        pytest.skip("The host does not permit creating a test directory junction")
    try:
        assert getattr(alias.lstat(), "st_reparse_tag", 0) == 0xA0000003
        with pytest.raises(restore_tool.RestoreError) as raised:
            _restore(restore_tool, archive_file, target, storage_root=alias / "objects")
        assert raised.value.code == "restore_storage_invalid"
        assert not (real / "objects").exists() and not any(_counts(target).values())
        assert keep.read_bytes() == b"junction target must not be removed"
    finally:
        # Remove this exact junction itself; never recurse into its target.
        assert alias.parent.resolve() == tmp_path.resolve()
        assert getattr(alias.lstat(), "st_reparse_tag", 0) == 0xA0000003
        os.rmdir(alias)
    assert not alias.exists() and keep.read_bytes() == b"junction target must not be removed"


def test_restore_rejects_oversized_object_before_any_write(restore_tool, archive_file, target, monkeypatch):
    monkeypatch.setattr(restore_tool, "MAX_OBJECT_BYTES", 1)
    def no_write(*args, **kwargs):
        pytest.fail("An oversized object must be rejected before writing any object")
    monkeypatch.setattr(restore_tool.LocalStorage, "put", no_write)
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target)
    assert raised.value.code == "restore_object_too_large"
    assert not any(_counts(target).values()) and not _object_files(target.storage_root)


def test_restore_refuses_wrong_schema_even_if_alembic_revision_is_current(restore_tool, archive_file, target):
    with target.engine.begin() as connection:
        connection.execute(text("ALTER TABLE notes RENAME COLUMN title TO invalid_title"))
    try:
        with pytest.raises(restore_tool.RestoreError) as raised:
            _restore(restore_tool, archive_file, target)
        assert raised.value.code == "restore_schema_invalid"
        assert not _object_files(target.storage_root)
    finally:
        with target.engine.begin() as connection:
            connection.execute(text("ALTER TABLE notes RENAME COLUMN invalid_title TO title"))


def test_restore_refuses_wrong_migration_revision(restore_tool, archive_file, target):
    with target.engine.begin() as connection:
        previous = connection.scalar(text("SELECT version_num FROM alembic_version"))
        connection.execute(text("UPDATE alembic_version SET version_num = 'not-current'"))
    try:
        with pytest.raises(restore_tool.RestoreError) as raised:
            _restore(restore_tool, archive_file, target)
        assert raised.value.code == "restore_migration_mismatch"
        assert not any(_counts(target).values()) and not _object_files(target.storage_root)
    finally:
        with target.engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = :previous"), {"previous": previous})


def test_restore_refuses_corrupt_archive_before_writing(restore_tool, archive_file, target, tmp_path):
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive_file) as source, zipfile.ZipFile(corrupt, "w") as output:
        for entry in source.infolist():
            data = source.read(entry.filename)
            output.writestr(entry.filename, data + b" " if entry.filename == "data.json" else data)
    with pytest.raises(restore_tool.RestoreError):
        _restore(restore_tool, corrupt, target)
    assert not any(_counts(target).values()) and not _object_files(target.storage_root)


def test_restore_requires_new_nontrivial_password(restore_tool, archive_file, target):
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target, password="short")
    assert raised.value.code == "restore_password_invalid"
    assert not any(_counts(target).values()) and not _object_files(target.storage_root)


def test_restore_object_write_failure_rolls_back_rows_and_cleans_written_objects(
    restore_tool, archive_file, target, monkeypatch,
):
    original_put = restore_tool.LocalStorage.put
    attempted = []
    def write_then_fail(store, key, data):
        original_put(store, key, data)
        attempted.append(key)
        raise OSError("synthetic storage error after durable write")
    monkeypatch.setattr(restore_tool.LocalStorage, "put", write_then_fail)
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target)
    assert raised.value.code == "restore_storage_unavailable" and attempted
    assert not any(_counts(target).values()) and not _object_files(target.storage_root)


def test_restore_insert_failure_rolls_back_and_removes_only_its_objects(
    restore_tool, archive_file, target, monkeypatch,
):
    real_engine = restore_tool.create_engine
    def failing_engine(*args, **kwargs):
        result = real_engine(*args, **kwargs)
        @event.listens_for(result, "before_cursor_execute")
        def fail_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO AI_RUNS"):
                raise SQLAlchemyError("synthetic insert failure")
        return result
    monkeypatch.setattr(restore_tool, "create_engine", failing_engine)
    with pytest.raises(restore_tool.RestoreError):
        _restore(restore_tool, archive_file, target)
    assert not any(_counts(target).values()) and not _object_files(target.storage_root)


@pytest.mark.parametrize("commit_succeeded", [False, True])
def test_restore_uncertain_commit_retains_every_object_and_rejects_blind_retry(
    restore_tool, archive_file, target, monkeypatch, commit_succeeded,
):
    def uncertain_commit(connection):
        if commit_succeeded:
            connection.commit()
        raise OSError("synthetic commit acknowledgement loss")
    monkeypatch.setattr(restore_tool, "_commit", uncertain_commit)
    with pytest.raises(restore_tool.RestoreError) as raised:
        _restore(restore_tool, archive_file, target)
    assert raised.value.code == "restore_commit_uncertain"
    assert len(_object_files(target.storage_root)) == 7
    counts = _counts(target)
    assert counts["users"] == int(commit_succeeded)
    assert counts["notes"] == (4 if commit_succeeded else 0)
    with pytest.raises(restore_tool.RestoreError) as retry:
        _restore(restore_tool, archive_file, target)
    assert retry.value.code in {"restore_storage_not_empty", "restore_database_not_empty"}
    assert len(_object_files(target.storage_root)) == 7
