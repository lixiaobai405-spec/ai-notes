"""Real c2 backups and migration rehearsals against owned PostgreSQL 16 databases.

No application API, real OSS, model provider, existing data, or private env file
is required. Old schemas are built by Alembic, never create_all plus stamp.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import MetaData, create_engine, inspect, select, text

from app.shared.db import engine as configured_engine
from app.shared.security import hasher, verify_password


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
LEGACY = "c2e41a9f3b67"
NORMALIZED = {"sessions", "file_tickets", "password_resets", "ai_credentials", "jobs"}
NEW_TABLES = {"pdf_imports", "ai_conversations", "ai_runs", "ai_steps", "ai_saved_notes"}
OWNED = re.compile(r"notes_restore_site_legacy_(?:source|target)_[0-9a-f]{24}")
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_database():
    """Override the global TRUNCATE fixture: this module owns separate databases."""
    yield


def _subprocess_env(database_url):
    # An allowlist avoids inheriting credentials, application configuration, and
    # PostgreSQL service overrides from the developer's shell.
    names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES"}
    result = {key: value for key, value in os.environ.items() if key.upper() in names}
    result.update(DATABASE_URL=database_url, APP_ENV="development", APP_ORIGIN="http://testserver",
                  STORAGE_BACKEND="local", DEEPSEEK_API_KEY="", PYTHONIOENCODING="utf-8")
    return result


def _alembic(database, *arguments):
    # env.py imports app.shared.db.engine. A fresh interpreter is mandatory;
    # changing Config's URL in the current process would migrate the wrong DB.
    # Disable Settings' default .env reader before any engine is constructed.
    program = (
        "from app.shared.config import Settings; "
        "Settings.model_config['env_file'] = None; "
        "from alembic.config import CommandLine; "
        "CommandLine().main()"
    )
    result = subprocess.run([sys.executable, "-c", program, *arguments], cwd=BACKEND,
                            env=_subprocess_env(database.url), capture_output=True,
                            text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def legacy_tools():
    directory = str(ROOT / "scripts")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return SimpleNamespace(tool=importlib.import_module("site_backup"),
                           support=importlib.import_module("site_backup_support"))


@pytest.fixture(scope="module")
def legacy_databases(tmp_path_factory, legacy_tools):
    container = os.environ.get("SITE_BACKUP_TEST_CONTAINER")
    if not container:
        pytest.skip("Set SITE_BACKUP_TEST_CONTAINER for real PostgreSQL 16 legacy backups")
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container)
    url = configured_engine.url
    assert url.drivername == "postgresql+psycopg"
    assert url.host in {"localhost", "127.0.0.1", "::1"} and url.port in {5432, 55432}
    assert (url.database, url.username, url.password) == ("notes_test", "notes", "notes")
    # Confirm the Docker socket and Python connection identify the same test
    # cluster before creating any database, including the CI port mapping.
    checked = legacy_tools.support.DockerPostgres(configured_engine, container).check()
    assert checked["pg_major"] == 16
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    owned = []
    with admin.connect() as connection:
        assert 160000 <= int(connection.scalar(text("SHOW server_version_num"))) < 170000

    def create(kind="target", *, old=False):
        assert kind in {"source", "target"}
        name = f"notes_restore_site_legacy_{kind}_{uuid.uuid4().hex[:24]}"
        assert OWNED.fullmatch(name) and len(name) <= 63
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        database_engine = create_engine(url.set(database=name))
        directory = tmp_path_factory.mktemp(name)
        storage = directory / "objects"
        storage.mkdir()
        database = SimpleNamespace(name=name, engine=database_engine, storage_root=storage,
                                   directory=directory, container=container,
                                   url=url.set(database=name).render_as_string(hide_password=False))
        owned.append(database)
        if old:
            _alembic(database, "upgrade", LEGACY)
            assert _revision(database) == LEGACY
            assert len(inspect(database.engine).get_table_names()) == 22
        return database

    try:
        yield create
    finally:
        for database in reversed(owned):
            assert OWNED.fullmatch(database.name)
            assert database.engine.url.database == database.name
            database.engine.dispose()
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database.name}" WITH (FORCE)'))
        admin.dispose()


def _revision(database):
    with database.engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _metadata(database):
    metadata = MetaData()
    metadata.reflect(database.engine)
    return metadata


def _rows(database, columns=None):
    metadata = _metadata(database)
    with database.engine.connect() as connection:
        result = {}
        for name in sorted(columns if columns is not None else metadata.tables):
            table = metadata.tables[name]
            selected = [table.c[key] for key in columns[name]] if columns is not None else list(table.c)
            result[name] = [dict(row) for row in connection.execute(
                select(*selected).order_by(*table.primary_key.columns)).mappings()]
        return result


def _schema(database):
    inspector = inspect(database.engine)
    return {name: {
        "columns": [(column["name"], str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(name)],
        "checks": inspector.get_check_constraints(name),
        "foreign_keys": inspector.get_foreign_keys(name),
        "unique": inspector.get_unique_constraints(name),
        "indexes": inspector.get_indexes(name),
    } for name in sorted(inspector.get_table_names())}


def _seed(database):
    metadata = _metadata(database)
    contents = {}
    passwords = {}
    identifiers = SimpleNamespace(users=[], notes=[], files=[], received=None, optional=[])
    with database.engine.begin() as connection:
        def insert(table_name, **values):
            table = metadata.tables[table_name]
            if "id" in table.c:
                values.setdefault("id", uuid.uuid4())
            for key in ("created_at", "updated_at"):
                if key in table.c:
                    values.setdefault(key, NOW)
            if "revision" in table.c:
                values.setdefault("revision", 1)
            connection.execute(table.insert().values(**values))
            return values.get("id")

        def file(owner, label, *, status="ready", data=None):
            data = data if data is not None else ("私密对象-" + label).encode()
            key = f"{owner}/{uuid.uuid4()}/{label}"
            present = status in {"ready", "processing"}
            identifier = insert("files", owner_id=owner, object_key=key, filename=label + ".png",
                                mime="image/png", expected_bytes=len(data),
                                bytes=len(data) if present else None,
                                sha256=hashlib.sha256(data).hexdigest() if present else None,
                                status=status)
            if present:
                contents[key] = data
            return identifier, key, data

        for index, role in enumerate(("admin", "user", "user")):
            password = f"legacy-only-password-{index}"
            owner = insert("users", username=f"legacy-{index}", display_name=f"私密用户{index}",
                           role=role, status="active", password_hash=hasher.hash(password))
            passwords[owner] = password
            identifiers.users.append(owner)
            folder = insert("folders", owner_id=owner, name=f"目录{index}")
            child = insert("folders", owner_id=owner, name="子目录", parent_id=folder)
            notebook = insert("notebooks", owner_id=owner, name="私密笔记本", folder_id=child)
            tag = insert("tags", owner_id=owner, name="期末复习")
            note = insert("notes", owner_id=owner, title=f"用户{index}的私密正文", kind="text",
                          notebook_id=notebook, body_json={"type": "doc", "private": index},
                          plain_text=f"只属于账号{index}的正文", revision=2)
            identifiers.notes.append(note)
            insert("note_tags", owner_id=owner, note_id=note, tag_id=tag)
            image = insert("notes", owner_id=owner, title="私密图片", kind="image",
                           notebook_id=notebook, body_json={}, plain_text="")
            original, key, _ = file(owner, f"original-{index}")
            identifiers.files.append(original)
            insert("note_pages", owner_id=owner, note_id=image, file_id=original, position=0)
            for kind in ("reading", "thumbnail"):
                raw = f"legacy-{index}-{kind}".encode()
                variant_key = f"{owner}/{original}/{kind}"
                insert("file_variants", file_id=original, kind=kind, object_key=variant_key, bytes=len(raw))
                contents[variant_key] = raw
            # Historical page references have no current note_pages row. Their
            # files/variants still belong to the whole-site object inventory.
            historical, historical_key, _ = file(owner, f"historical-{index}")
            historical_reading_key = f"{owner}/{historical}/reading"
            historical_reading = f"legacy-historical-{index}-reading".encode()
            insert("file_variants", file_id=historical, kind="reading", object_key=historical_reading_key,
                   bytes=len(historical_reading))
            contents[historical_reading_key] = historical_reading
            insert("note_versions", owner_id=owner, note_id=image,
                   snapshot={"title": "旧标题", "kind": "image", "pages": [
                       {"file_id": str(historical), "object_key": historical_key, "position": 0}]})
            insert("note_versions", owner_id=owner, note_id=note,
                   snapshot={"title": "历史正文", "plain_text": "修改前正文", "body_json": {}})
            insert("notes", owner_id=owner, title="回收站保留", kind="text", body_json={},
                   plain_text="已删除但应保留", deleted_at=NOW - timedelta(days=1))
            insert("sessions", user_id=owner, token_hash=uuid.uuid4().hex, csrf_hash=uuid.uuid4().hex,
                   expires_at=NOW + timedelta(days=1), revoked_at=NOW if index == 2 else None)
            insert("password_resets", user_id=owner, token_hash=uuid.uuid4().hex,
                   expires_at=NOW + timedelta(days=1))
            insert("file_tickets", owner_id=owner, file_id=original, token_hash=uuid.uuid4().hex,
                   expires_at=NOW + timedelta(hours=1))
            insert("ai_credentials", owner_id=owner, provider="legacy-provider", ciphertext=b"x" * 48,
                   key_version=1, key_suffix="fake", available_models=[{"id": "offline-model"}])
            job = insert("jobs", owner_id=owner, kind="image.process",
                         status=("queued", "running", "succeeded")[index],
                         dedupe_key=f"legacy-job-{index}", payload={"private": index},
                         attempts=1, max_attempts=3, run_after=NOW,
                         result={"retained": True} if index == 2 else None,
                         lease_until=NOW + timedelta(minutes=1) if index == 1 else None,
                         worker_token="synthetic-worker" if index == 1 else None)
            event = insert("outbox_events", user_id=owner, job_id=job, kind="job.completed",
                           payload={"private": "持久化事件"}, delivered_at=NOW if index == 2 else None)
            insert("notifications", user_id=owner, event_id=event, message="历史通知保留",
                   read_at=NOW if index == 2 else None)
        owner = identifiers.users[0]
        first_image = connection.scalar(select(metadata.tables["notes"].c.id).where(
            metadata.tables["notes"].c.owner_id == owner, metadata.tables["notes"].c.kind == "image"))
        for status, upload_status in (("uploading", "pending"), ("expired", "expired"), ("rejected", None)):
            identifier, key, _ = file(owner, "optional-" + status, status=status)
            identifiers.optional.append(key)
            if upload_status:
                insert("upload_sessions", owner_id=owner, file_id=identifier, note_id=first_image,
                       request_key=uuid.uuid4().hex, status=upload_status, expires_at=NOW + timedelta(hours=1))
        received, key, raw = file(owner, "received-upload", status="uploading")
        contents[key] = raw
        identifiers.received = key
        file(owner, "processing-original", status="processing")
        insert("upload_sessions", owner_id=owner, file_id=received, note_id=first_image,
               request_key=uuid.uuid4().hex, status="received", expires_at=NOW + timedelta(hours=1))
        insert("invite_codes", creator_id=owner, code_hash=uuid.uuid4().hex, label="历史邀请",
               max_uses=5, used_count=2, disabled=False, expires_at=NOW + timedelta(days=30))
        insert("audit_logs", actor_id=owner, action="legacy.fixture", request_id="synthetic-only",
               details={"private": "历史审计"})
        insert("rate_buckets", key="legacy-only-bucket", count=2, expires_at=NOW + timedelta(minutes=1))
    for key, raw in contents.items():
        path = database.storage_root.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    database.contents, database.passwords, database.ids = contents, passwords, identifiers
    database.original_rows = _rows(database)
    database.original_schema = _schema(database)
    assert len(database.original_rows) == 22 and all(database.original_rows.values())
    return database


@pytest.fixture(scope="module")
def legacy_source(legacy_databases):
    return _seed(legacy_databases("source", old=True))


def _backup(tools, source, output, **overrides):
    arguments = dict(database_url=source.url, postgres_container=source.container, output=output,
                     storage=tools.support.LocalBackupStorage(source.storage_root),
                     maintenance_confirmed=True, schema_revision=LEGACY)
    arguments.update(overrides)
    return tools.tool.backup_site(**arguments)


def _restore(tools, bundle, target):
    return tools.tool.restore_site_backup(bundle, target.url, target.container, target.storage_root,
                                         isolated_confirmed=True)


@pytest.fixture(scope="module")
def legacy_bundle(legacy_tools, legacy_source, tmp_path_factory):
    output = tmp_path_factory.mktemp("legacy-backup") / "bundle"
    _backup(legacy_tools, legacy_source, output)
    return output


def _assert_source_unchanged(source):
    assert _revision(source) == LEGACY
    assert _rows(source) == source.original_rows
    assert _schema(source) == source.original_schema
    assert _stored_objects(source) == source.contents


def _stored_objects(database):
    return {path.relative_to(database.storage_root).as_posix(): path.read_bytes()
            for path in database.storage_root.rglob("*") if path.is_file()}


def _assert_restored(source, target, result):
    rows = _rows(target)
    assert set(rows) == set(source.original_rows) and len(rows) == 22
    assert _revision(target) == LEGACY
    for name in rows.keys() - NORMALIZED:
        assert rows[name] == source.original_rows[name], name
    for user in rows["users"]:
        assert verify_password(user["password_hash"], source.passwords[user["id"]])
    assert {user["role"] for user in rows["users"]} == {"admin", "user"}
    assert {row["owner_id"] for row in rows["notes"]} == set(source.ids.users)
    assert {row["owner_id"] for row in rows["files"]} == set(source.ids.users)
    assert all(row["revoked_at"] is not None for row in rows["sessions"])
    assert all(not rows[name] for name in ("file_tickets", "password_resets", "ai_credentials"))
    for old in source.original_rows["sessions"]:
        restored = next(row for row in rows["sessions"] if row["id"] == old["id"])
        assert {key: value for key, value in restored.items() if key != "revoked_at"} == {
            key: value for key, value in old.items() if key != "revoked_at"}
        if old["revoked_at"] is not None:
            assert restored == old
    for old in source.original_rows["jobs"]:
        restored = next(row for row in rows["jobs"] if row["id"] == old["id"])
        if old["status"] == "succeeded":
            assert restored == old
        else:
            assert restored == {**old, "status": "failed", "error_code": "site_restore_interrupted",
                                "lease_until": None, "worker_token": None}
    assert result["original_fingerprints_verified"] is True
    assert result["normalization"] == {"sessions": 2, "file_tickets": 3, "password_resets": 3,
                                       "ai_credentials": 3, "jobs": 2}
    assert result["missing_optional_objects"] == 3
    assert result["objects"] == len(source.contents)
    assert _stored_objects(target) == source.contents
    _assert_source_unchanged(source)
    return rows


def test_legacy_local_roundtrip_preserves_all_old_tables_and_migrates_only_restored_copy(
    legacy_tools, legacy_source, legacy_bundle, legacy_databases,
):
    target = legacy_databases()
    result = _restore(legacy_tools, legacy_bundle, target)
    restored = _assert_restored(legacy_source, target, result)
    manifest = legacy_tools.tool.verify_site_backup(legacy_bundle)
    assert manifest["format_version"] == 1 and manifest["schema_revision"] == LEGACY
    assert manifest["source_storage"] == "local" and len(manifest["tables"]) == 22
    assert {name: item["count"] for name, item in manifest["tables"].items()} == {
        name: len(rows) for name, rows in legacy_source.original_rows.items()}
    assert all(reference.startswith(("files:", "file_variants:"))
               for entry in manifest["objects"] for reference in entry["references"])
    for entry in manifest["objects"]:
        if entry["status"] == "present":
            raw = legacy_source.contents[entry["key"]]
            assert entry["size"] == len(raw) and entry["sha256"] == hashlib.sha256(raw).hexdigest()
            assert (legacy_bundle / entry["path"]).read_bytes() == raw
        else:
            assert entry["key"] in legacy_source.ids.optional and not entry["mandatory"]
    columns = {name: list(rows[0]) for name, rows in legacy_source.original_rows.items()}
    # Use the actual four repository migrations, keeping the old row projection.
    for revision in ("d89f74a2136c", "16bd16083e39", "63b9d8343e02", "2ef16e7ca892"):
        _alembic(target, "upgrade", revision)
        assert _revision(target) == revision
    assert "No new upgrade operations detected" in _alembic(target, "check")
    assert _rows(target, columns) == {**restored, "alembic_version": [{"version_num": "2ef16e7ca892"}]}
    current = _rows(target)
    assert set(current) - set(restored) == NEW_TABLES
    assert all(not current[name] for name in NEW_TABLES)
    assert all(row["pdf_page_number"] is None for row in current["note_pages"])
    assert _stored_objects(target) == legacy_source.contents
    _assert_source_unchanged(legacy_source)


class _OSSBody:
    def __init__(self, raw):
        self.raw, self.closed = raw, False

    def iter_bytes(self, **_):
        yield self.raw[:3]
        yield self.raw[3:]

    def close(self):
        self.closed = True


class _FakeOSSClient:
    """Network-free SDK surface; the production backup adapter remains real."""
    def __init__(self, contents, *, denied=None, missing=None):
        self.contents, self.denied, self.missing = contents, denied, missing
        self.reads, self.bodies = [], []

    def head_object(self, request):
        from alibabacloud_oss_v2.exceptions import ServiceError
        key = request.key.removeprefix("private/")
        assert request.bucket == "legacy-test-bucket"
        if key == self.denied or key == self.missing or key not in self.contents:
            denied = key == self.denied
            raise ServiceError(status_code=403 if denied else 404,
                               code="AccessDenied" if denied else "NoSuchKey", request_id="synthetic",
                               message="synthetic-only", ec="synthetic", timestamp="synthetic",
                               request_target="https://synthetic.invalid/private-key")
        return SimpleNamespace(content_length=len(self.contents[key]), version_id="version-selected-1",
                               etag='"legacy-fixed-etag"')

    def get_object(self, request):
        key = request.key.removeprefix("private/")
        assert request.version_id == "version-selected-1" and request.if_match is None
        self.reads.append(key)
        body = _OSSBody(self.contents[key])
        self.bodies.append(body)
        return SimpleNamespace(content_length=len(self.contents[key]), version_id="version-selected-1",
                               etag='"legacy-fixed-etag"', body=body)


def _oss_storage(tools, source, **kwargs):
    client = _FakeOSSClient(source.contents, **kwargs)
    cfg = SimpleNamespace(oss_bucket="legacy-test-bucket", oss_prefix="private/",
                          upload_max_bytes=1024, pdf_max_bytes=1024)
    return tools.support.OSSBackupStorage(cfg, client=client), client


def test_legacy_oss_roundtrip_uses_selected_version_and_records_etag(
    legacy_tools, legacy_source, legacy_databases, tmp_path,
):
    storage, client = _oss_storage(legacy_tools, legacy_source)
    bundle = tmp_path / "oss-bundle"
    _backup(legacy_tools, legacy_source, bundle, storage=storage)
    manifest = legacy_tools.tool.verify_site_backup(bundle)
    assert manifest["source_storage"] == "oss" and manifest["schema_revision"] == LEGACY
    for entry in manifest["objects"]:
        if entry["status"] == "present":
            assert (entry["version_id"], entry["etag"]) == ("version-selected-1", '"legacy-fixed-etag"')
    assert set(client.reads) == set(legacy_source.contents)
    assert all(body.closed for body in client.bodies)
    target = legacy_databases()
    _assert_restored(legacy_source, target, _restore(legacy_tools, bundle, target))


@pytest.mark.parametrize("failure", ["mandatory_original", "mandatory_received", "optional_denied"])
def test_legacy_oss_missing_required_and_permission_errors_do_not_publish(
    legacy_tools, legacy_source, tmp_path, failure,
):
    if failure == "optional_denied":
        kwargs, expected = {"denied": legacy_source.ids.optional[0]}, "site_storage_unavailable"
    else:
        key = legacy_source.ids.received if failure == "mandatory_received" else next(iter(legacy_source.contents))
        kwargs, expected = {"missing": key}, "backup_object_missing"
    storage, _ = _oss_storage(legacy_tools, legacy_source, **kwargs)
    output = tmp_path / "rejected-oss-backup"
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _backup(legacy_tools, legacy_source, output, storage=storage)
    assert error.value.code == expected and not output.exists()
    _assert_source_unchanged(legacy_source)


@pytest.mark.parametrize("kind", ["original", "variant", "received"])
def test_legacy_local_missing_required_objects_are_rejected(
    legacy_tools, legacy_source, tmp_path, kind,
):
    root = tmp_path / "copied-storage"
    shutil.copytree(legacy_source.storage_root, root)
    if kind == "received":
        key = legacy_source.ids.received
    elif kind == "variant":
        key = next(key for key in legacy_source.contents if key.endswith("/reading"))
    else:
        key = next(iter(legacy_source.contents))
    root.joinpath(*key.split("/")).unlink()
    output = tmp_path / "missing-local-backup"
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _backup(legacy_tools, legacy_source, output, storage=legacy_tools.support.LocalBackupStorage(root))
    assert error.value.code == "backup_object_missing" and not output.exists()
    _assert_source_unchanged(legacy_source)


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_OBJECT_BYTES", "MAX_TOTAL_BYTES", "MAX_DUMP_BYTES", "MAX_MANIFEST_BYTES"])
def test_legacy_profile_keeps_backup_resource_limits(
    legacy_tools, legacy_source, tmp_path, monkeypatch, limit,
):
    monkeypatch.setattr(legacy_tools.tool, limit, 1)
    output = tmp_path / "resource-limit-backup"
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _backup(legacy_tools, legacy_source, output)
    assert error.value.code == "backup_limit_exceeded" and not output.exists()
    _assert_source_unchanged(legacy_source)


def test_legacy_default_head_and_unknown_explicit_revision_are_rejected(
    legacy_tools, legacy_source, tmp_path,
):
    arguments = dict(database_url=legacy_source.url, postgres_container=legacy_source.container,
                     storage=legacy_tools.support.LocalBackupStorage(legacy_source.storage_root),
                     maintenance_confirmed=True)
    for revision, code in ((None, "backup_schema_invalid"), ("unknown-revision", "backup_schema_unsupported")):
        output = tmp_path / ("default-head" if revision is None else "unknown-profile")
        if revision is not None:
            arguments["schema_revision"] = revision
        with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
            legacy_tools.tool.backup_site(output=output, **arguments)
        assert error.value.code == code and not output.exists()
    _assert_source_unchanged(legacy_source)


@pytest.mark.parametrize("drift", ["forged_head_stamp", "missing_tables", "column_type", "constraint"])
def test_legacy_catalog_rejects_forged_stamp_and_structure_drift(
    legacy_tools, legacy_databases, tmp_path, drift,
):
    source = legacy_databases("source", old=True)
    with source.engine.begin() as connection:
        if drift == "forged_head_stamp":
            connection.execute(text("UPDATE alembic_version SET version_num='2ef16e7ca892'"))
        elif drift == "missing_tables":
            connection.execute(text("DROP TABLE ai_credentials, rate_buckets"))
        elif drift == "column_type":
            connection.execute(text("ALTER TABLE users ALTER COLUMN username TYPE text"))
        else:
            connection.execute(text("ALTER TABLE users DROP CONSTRAINT user_role"))
    before, schema = _rows(source), _schema(source)
    output = tmp_path / "schema-drift-backup"
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _backup(legacy_tools, source, output, schema_revision=None if drift == "forged_head_stamp" else LEGACY)
    assert error.value.code == "backup_schema_invalid" and not output.exists()
    assert _rows(source) == before and _schema(source) == schema


@pytest.mark.parametrize("damage", ["dump", "object", "unknown_revision", "revision_alias", "null_revision", "head_revision"])
def test_legacy_corrupt_bundle_is_rejected_before_target_writes(
    legacy_tools, legacy_bundle, legacy_databases, tmp_path, damage,
):
    bundle = tmp_path / "corrupt-bundle"
    shutil.copytree(legacy_bundle, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    revisions = {"unknown_revision": "unknown-revision", "revision_alias": "head",
                 "null_revision": None, "head_revision": "2ef16e7ca892"}
    if damage in revisions:
        manifest["schema_revision"] = revisions[damage]
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        entry = manifest["dump"] if damage == "dump" else next(
            item for item in manifest["objects"] if item["status"] == "present")
        (bundle / entry["path"]).write_bytes(b"corrupt-synthetic-data")
    target = legacy_databases()
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _restore(legacy_tools, bundle, target)
    assert error.value.code == "backup_invalid"
    assert inspect(target.engine).get_table_names() == []
    assert _stored_objects(target) == {}


@pytest.mark.parametrize("damage", ["consistent_omission", "original_fingerprint"])
def test_legacy_post_restore_verification_fails_before_normalization_and_keeps_marker(
    legacy_tools, legacy_source, legacy_bundle, legacy_databases, tmp_path, damage,
):
    bundle = tmp_path / "incomplete-bundle"
    shutil.copytree(legacy_bundle, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "consistent_omission":
        item = next(item for item in manifest["objects"] if item["status"] == "present")
        manifest["objects"].remove(item)
        (bundle / item["path"]).unlink()
    else:
        manifest["tables"]["notes"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    legacy_tools.tool.verify_site_backup(bundle)
    target = legacy_databases()
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _restore(legacy_tools, bundle, target)
    assert error.value.code == "restore_verification_failed"
    assert (target.storage_root / ".site-restore-incomplete").is_file()
    assert _rows(target) == legacy_source.original_rows
    _assert_source_unchanged(legacy_source)


def test_legacy_restore_never_overwrites_a_nonempty_target(
    legacy_tools, legacy_bundle, legacy_databases,
):
    target = legacy_databases()
    with target.engine.begin() as connection:
        connection.execute(text("CREATE TABLE keep_private_data (value text PRIMARY KEY)"))
        connection.execute(text("INSERT INTO keep_private_data VALUES ('keep-me')"))
    with pytest.raises(legacy_tools.tool.SiteBackupError) as error:
        _restore(legacy_tools, legacy_bundle, target)
    assert error.value.code == "restore_target_not_empty"
    with target.engine.connect() as connection:
        assert connection.scalar(text("SELECT value FROM keep_private_data")) == "keep-me"
    assert _stored_objects(target) == {}


def test_legacy_real_cli_uses_private_url_files_and_no_application_database(
    legacy_tools, legacy_source, legacy_databases, tmp_path,
):
    target = legacy_databases()
    source_file, target_file = tmp_path / "source-url.txt", tmp_path / "target-url.txt"
    source_file.write_text(legacy_source.url, encoding="utf-8")
    target_file.write_text(target.url, encoding="utf-8")
    os.chmod(source_file, 0o600)
    os.chmod(target_file, 0o600)
    bundle = tmp_path / "cli-bundle"
    environment = _subprocess_env("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")

    def run(*arguments, success=True):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "site_backup.py"), *arguments],
                                cwd=tmp_path, env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=90)
        diagnostic = result.stdout + result.stderr
        assert legacy_source.url not in diagnostic and target.url not in diagnostic
        assert "notes:notes@" not in diagnostic and "Traceback" not in diagnostic
        assert result.returncode == (0 if success else 1), diagnostic
        value = json.loads(result.stdout if success else result.stderr)
        assert value["ok"] is success
        return value

    common = ("--database-url-file", str(source_file), "--postgres-container", legacy_source.container,
              "--storage-backend", "local", "--storage-root", str(legacy_source.storage_root),
              "--maintenance-confirmed")
    default = run("backup", *common, "--output", str(tmp_path / "default-head"), success=False)
    assert default["error"] == "backup_schema_invalid"
    run("backup", *common, "--output", str(bundle), "--schema-revision", LEGACY)
    verified = run("verify", str(bundle))
    assert verified["tables"] == 22 and verified["objects"] == len(legacy_source.contents)
    report = run("restore", str(bundle), "--database-url-file", str(target_file),
                 "--postgres-container", target.container, "--storage-root", str(target.storage_root),
                 "--isolated-confirmed")
    _assert_restored(legacy_source, target, report)
