"""Actual operator CLI on a disposable PostgreSQL database and temporary files."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.modules.files.models import File, UploadSession
from app.modules.files.retention import collect_references
from app.modules.notes.models import Note
from app.shared.db import engine, SessionLocal, utcnow
from app.shared.models import User

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.file_gc import DatabaseReferences
from scripts.file_gc_common import FileGCError
from scripts.file_gc_state import PrivateState


@pytest.fixture
def workspace(tmp_path):
    live = tmp_path / "live"; live.mkdir(mode=0o700)
    state, config = PrivateState(tmp_path / "state"), PrivateState(tmp_path / "config")
    with SessionLocal.begin() as db:
        owner = db.scalar(select(User.id).where(User.username == "admin"))
        file_id = uuid4(); key = f"{owner}/{file_id}/original"
        db.add(File(id=file_id, owner_id=owner, object_key=key, filename="protected.bin", mime="image/png",
                    expected_bytes=9, bytes=9, status="ready"))
    def put(key, value, age=8):
        target = live.joinpath(*key.split("/")); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value); modified = (utcnow() - timedelta(days=age)).timestamp(); os.utime(target, (modified, modified))
        return target
    protected = put(key, b"protected")
    orphan_key = f"{owner}/{uuid4()}/{uuid4()}-reading.jpg"
    orphan = put(orphan_key, b"isolated orphan bytes")
    unknown = put("operator/unknown.txt", b"unknown retained", age=90)
    config.write("config.json", {"database_url_env": "GC_TEST_DATABASE_URL", "state_root": str(state.root),
                                "storage": {"backend": "local", "root": str(live)}})
    env = {**os.environ, "GC_TEST_DATABASE_URL": engine.url.render_as_string(hide_password=False),
           "DEEPSEEK_API_KEY": "", "STORAGE_BACKEND": "local"}
    def cli(*args, expected=0):
        process = subprocess.run([sys.executable, "-m", "scripts.file_gc", "--config", str(config.root / "config.json"), *args],
                                 cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True, timeout=40)
        assert process.returncode == expected, (process.stdout, process.stderr)
        value = json.loads(process.stdout)
        assert "postgresql" not in process.stdout and str(owner) not in process.stdout and orphan_key not in process.stdout
        assert not process.stderr
        return value
    return dict(cli=cli, live=live, state=state, protected=protected, orphan=orphan, unknown=unknown, owner=owner,
                orphan_key=orphan_key, file_id=file_id)


def fingerprint():
    with engine.connect() as connection:
        return collect_references(connection)["fingerprint"]


def test_actual_default_plan_apply_and_restore_preserve_registered_objects_and_database(workspace):
    w = workspace; before = fingerprint()
    plan = w["cli"]()
    assert plan["status"] == "planned" and plan["referenced"] == 1 and plan["unknown"] == 1 and plan["candidates"] == 1
    assert w["orphan"].exists() and w["protected"].exists()
    denied = w["cli"]("apply", "--plan", plan["plan"], "--digest", plan["digest"], expected=1)
    assert denied["code"] == "gc_maintenance_required" and w["orphan"].exists()
    applied = w["cli"]("apply", "--plan", plan["plan"], "--digest", plan["digest"], "--maintenance-confirmed", "--writers-stopped")
    assert applied["status"] == "complete" and applied["deleted"] == 1 and not w["orphan"].exists()
    assert w["protected"].read_bytes() == b"protected" and w["unknown"].read_bytes() == b"unknown retained"
    status = w["cli"]("status", "--bundle", applied["bundle"])
    assert status["digest"] == applied["digest"] and status["deleted"] == 1
    restored = w["cli"]("restore", "--bundle", applied["bundle"], "--digest", applied["digest"],
                        "--maintenance-confirmed", "--writers-stopped")
    assert restored["status"] == "restored" and restored["restored"] == 1
    assert w["orphan"].read_bytes() == b"isolated orphan bytes" and fingerprint() == before


def test_actual_apply_revalidates_new_references_and_active_uploads(workspace):
    w = workspace; plan = w["cli"]("plan")
    with SessionLocal.begin() as db:
        file_id = uuid4()
        db.add(File(id=file_id, owner_id=w["owner"], object_key=w["orphan_key"], filename="now-registered.bin",
                    mime="image/png", expected_bytes=21, status="ready"))
    failed = w["cli"]("apply", "--plan", plan["plan"], "--digest", plan["digest"], "--maintenance-confirmed", "--writers-stopped", expected=1)
    assert failed["code"] == "gc_references_changed" and w["orphan"].exists()
    with SessionLocal.begin() as db:
        note = Note(owner_id=w["owner"], title="pending", kind="image"); db.add(note); db.flush()
        db.add(UploadSession(owner_id=w["owner"], note_id=note.id, file_id=w["file_id"], request_key="gc-pending",
                             status="received", expires_at=utcnow() + timedelta(minutes=10)))
    plan = w["cli"]("plan")
    assert plan["active_writers"] == 1
    failed = w["cli"]("apply", "--plan", plan["plan"], "--digest", plan["digest"], "--maintenance-confirmed", "--writers-stopped", expected=1)
    assert failed["code"] == "gc_writers_active"


def test_database_guard_excludes_other_gc_state_roots_and_publication(workspace):
    references = DatabaseReferences(engine.url)
    with references.snapshot(writing=True):
        with pytest.raises(FileGCError, match="gc_locked"):
            with DatabaseReferences(engine.url).snapshot(writing=True):
                pytest.fail("second GC entered")
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(OperationalError):
                connection.execute(text("UPDATE public.files SET filename='must-not-publish' WHERE id=:id"), {"id": workspace["file_id"]})
    with SessionLocal() as db:
        assert db.get(File, workspace["file_id"]).filename == "protected.bin"
