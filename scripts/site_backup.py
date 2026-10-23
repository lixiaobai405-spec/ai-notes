"""Private operator backup and isolated restore of one complete AI Notes site.

Only trusted, operator-created bundles are accepted. This is deliberately not a
web upload/import endpoint. Callers stop every writer before backup, and keep
the separate restore environment offline until its report has been reviewed.
"""

import argparse
import base64
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from uuid import UUID


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "backend"))
sys.path.insert(0, str(REPOSITORY / "scripts"))

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import MetaData, create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.shared.db import Base  # noqa: E402
from app.shared import models as registered_models  # noqa: E402,F401
from site_backup_support import DockerPostgres, LocalBackupStorage, OSSBackupStorage, SiteBackupError  # noqa: E402


FORMAT_VERSION = 1
MAX_OBJECT_BYTES = 50 * 1024**2
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_DUMP_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 32 * 1024**2
MAX_ROWS = 1_000_000
MAX_OBJECTS = 100_000
CHUNK_BYTES = 256 * 1024
SCOPE = "postgresql_all_public_tables_and_referenced_objects_selected_versions"
TABLE_NAMES = set(Base.metadata.tables) | {"alembic_version"}
HEX = re.compile(r"[0-9a-f]{64}")
MANIFEST_FIELDS = {"format_version", "created_at", "schema_revision", "postgres_major",
                   "scope", "source_storage", "tables", "objects", "dump"}
OBJECT_FIELDS = {"key", "path", "mandatory", "references", "status", "size", "sha256",
                 "version_id", "etag", "reason"}


def _require(condition, code="backup_invalid"):
    if not condition:
        raise SiteBackupError(code)


def _revision():
    cfg = Config(str(REPOSITORY / "backend" / "alembic.ini"))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("script_location", str(REPOSITORY / "backend" / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _url(database_url, *, restore=False):
    code = "restore_target_invalid" if restore else "backup_source_invalid"
    try:
        url = make_url(database_url)
        _require(url.drivername == "postgresql+psycopg"
                 and url.host in {"localhost", "127.0.0.1", "::1"}
                 and url.port is not None and 1 <= url.port <= 65535
                 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", url.username or "")
                 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", url.database or "")
                 and not url.query, code)
        if restore:
            _require(re.fullmatch(r"notes_restore_site(?:_[a-z0-9_]+)?", url.database), code)
        return url
    except SiteBackupError:
        raise
    except (TypeError, ValueError, SQLAlchemyError):
        raise SiteBackupError(code) from None


def _engine(database_url, *, restore=False):
    url = _url(database_url, restore=restore)
    return create_engine(url, pool_pre_ping=True, connect_args={
        "hostaddr": "::1" if url.host == "::1" else "127.0.0.1",
        "password": url.password or "", "connect_timeout": 10,
        "options": "-c search_path=public -c timezone=UTC -c statement_timeout=120000 -c lock_timeout=5000",
        "application_name": "ai-notes-site-backup",
    })


def _no_aliases(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "backup_path_invalid")


def _path(value):
    raw = Path(value)
    _require(".." not in raw.parts, "backup_path_invalid")
    path = raw.absolute()
    _no_aliases(path)
    return path


def _separate(first, second, code="backup_path_invalid"):
    a, b = first.resolve(), second.resolve()
    _require(not a.is_relative_to(b) and not b.is_relative_to(a), code)


def _key(key):
    _require(isinstance(key, str) and 0 < len(key) <= 256
             and re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+", key)
             and all(part not in {".", ".."} for part in key.split("/")), "backup_object_key_invalid")
    return key


def _integer(value, maximum, *, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _regular_open(path):
    _no_aliases(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "backup_path_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    stream = os.fdopen(descriptor, "rb")
    try:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "backup_path_invalid")
        return stream
    except BaseException:
        stream.close()
        raise


def _hash_file(path, limit):
    with _regular_open(path) as stream:
        info = os.fstat(stream.fileno())
        _require(info.st_size <= limit, "backup_limit_exceeded")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(CHUNK_BYTES):
            size += len(chunk)
            _require(size <= limit, "backup_limit_exceeded")
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, (UUID, date, Decimal)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _json(value):
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _schema(connection, *, empty=False):
    schemas = connection.execute(text("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"))
    _require({row[0] for row in schemas} == {"public"}, "backup_schema_invalid")
    inspector = inspect(connection)
    names = set(inspector.get_table_names(schema="public"))
    unexpected = connection.execute(text("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind NOT IN ('r','i','t')
    """)).scalar_one()
    routines = connection.execute(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'" )).scalar_one()
    extra_types = connection.execute(text("""
        SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public' AND t.typrelid=0 AND t.typelem=0
    """)).scalar_one()
    large_objects = connection.execute(text("SELECT count(*) FROM pg_catalog.pg_largeobject_metadata")).scalar_one()
    extensions = connection.execute(text("SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql'")).scalar_one()
    triggers = connection.execute(text("""
        SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
    """)).scalar_one()
    _require(not any((unexpected, routines, extra_types, large_objects, extensions, triggers)), "backup_schema_invalid")
    _require(not names if empty else names == TABLE_NAMES,
             "restore_target_not_empty" if empty else "backup_schema_invalid")
    if empty:
        return None
    metadata = MetaData()
    metadata.reflect(connection, schema="public", only=sorted(TABLE_NAMES))
    _require(all(table.primary_key.columns for table in metadata.tables.values()), "backup_schema_invalid")
    versions = list(connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars())
    _require(versions == [_revision()], "backup_schema_invalid")
    return metadata


def _fingerprints(connection, metadata):
    result, total = {}, 0
    for name in sorted(TABLE_NAMES):
        table = metadata.tables[f"public.{name}"]
        statement = select(*sorted(table.c, key=lambda item: item.name)).order_by(*table.primary_key.columns)
        rows = connection.execute(statement.execution_options(stream_results=True)).mappings()
        digest, count = hashlib.sha256(), 0
        try:
            for row in rows:
                encoded = _json(dict(row))
                _require(len(encoded) <= MAX_MANIFEST_BYTES, "backup_limit_exceeded")
                count += 1
                total += 1
                _require(total <= MAX_ROWS, "backup_limit_exceeded")
                digest.update(encoded + b"\n")
        finally:
            rows.close()
        result[name] = {"count": count, "sha256": digest.hexdigest()}
    return result


def _references(connection):
    objects = {}

    def add(key, reference, mandatory, size=None, digest=None):
        _key(key)
        _require(size is None or _integer(size, MAX_OBJECT_BYTES, minimum=1), "backup_limit_exceeded")
        _require(digest is None or isinstance(digest, str) and HEX.fullmatch(digest), "backup_schema_invalid")
        item = objects.setdefault(key, {"key": key, "mandatory": False, "references": [],
                                       "expected_size": None, "expected_sha256": None})
        _require(len(objects) <= MAX_OBJECTS, "backup_limit_exceeded")
        for field, value in (("expected_size", size), ("expected_sha256", digest)):
            _require(value is None or item[field] is None or item[field] == value, "backup_object_mismatch")
            if value is not None:
                item[field] = value
        item["mandatory"] |= mandatory
        item["references"].append(reference)

    rows = connection.execute(text("""
        SELECT f.id,f.object_key,f.status,f.bytes,f.expected_bytes,f.sha256,u.status AS upload_status
        FROM public.files f LEFT JOIN public.upload_sessions u ON u.file_id=f.id
        ORDER BY f.id
    """).execution_options(stream_results=True)).mappings()
    for row in rows:
        required = row["status"] in {"ready", "processing"} or row["status"] == "uploading" and row["upload_status"] == "received"
        size = row["bytes"] if row["bytes"] is not None else row["expected_bytes"]
        add(row["object_key"], f"files:{row['id']}", required, size, row["sha256"])
    for row in connection.execute(text("SELECT id,object_key,bytes FROM public.file_variants ORDER BY id").execution_options(stream_results=True)).mappings():
        add(row["object_key"], f"file_variants:{row['id']}", True, row["bytes"])
    for row in connection.execute(text("SELECT id,source FROM public.ai_runs WHERE source IS NOT NULL ORDER BY id").execution_options(stream_results=True)).mappings():
        source = row["source"]
        _require(isinstance(source, dict), "backup_schema_invalid")
        pages = source.get("pages", [])
        _require(isinstance(pages, list), "backup_schema_invalid")
        for number, page in enumerate(pages):
            _require(isinstance(page, dict), "backup_schema_invalid")
            if "object_key" in page:
                add(page["object_key"], f"ai_runs:{row['id']}:pages:{number}", True, page.get("bytes"))
    return [objects[key] for key in sorted(objects)]


def _summary(manifest):
    return {"tables": len(manifest["tables"]), "rows": sum(item["count"] for item in manifest["tables"].values()),
            "objects": sum(item["status"] == "present" for item in manifest["objects"]),
            "missing_optional_objects": sum(item["status"] == "missing" for item in manifest["objects"])}


def backup_site(database_url, postgres_container, output, storage, *, maintenance_confirmed=False):
    _require(maintenance_confirmed is True, "backup_maintenance_required")
    engine = _engine(database_url)
    stage = None
    try:
        destination = _path(output)
        _require(not destination.exists(), "backup_destination_exists")
        if getattr(storage, "root", None) is not None:
            _separate(destination, _path(storage.root))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _no_aliases(destination.parent)
        stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        os.chmod(stage, 0o700)
        (stage / "objects").mkdir(mode=0o700)
        postgres = DockerPostgres(engine, postgres_container)
        checked = postgres.check()
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                metadata = _schema(connection)
                snapshot = connection.execute(text("SELECT pg_export_snapshot()" )).scalar_one()
                tables = _fingerprints(connection, metadata)
                references = _references(connection)
                postgres.dump(snapshot, stage / "database.dump", MAX_DUMP_BYTES)
                dump = {"path": "database.dump", **_hash_file(stage / "database.dump", MAX_DUMP_BYTES)}
                os.chmod(stage / "database.dump", 0o600)
                total, objects = dump["size"], []
                for reference in references:
                    relative = "objects/" + hashlib.sha256(reference["key"].encode("utf-8")).hexdigest()
                    target = stage / relative
                    try:
                        captured = storage.capture(reference["key"], target, MAX_OBJECT_BYTES)
                    except FileNotFoundError:
                        _require(not reference["mandatory"], "backup_object_missing")
                        _require(not target.exists(), "backup_object_mismatch")
                        captured = {"size": None, "sha256": None, "version_id": None, "etag": None}
                        status, reason = "missing", "not_found_optional"
                    else:
                        actual = _hash_file(target, MAX_OBJECT_BYTES)
                        _require(captured.get("size") == actual["size"] and captured.get("sha256") == actual["sha256"], "backup_object_mismatch")
                        _require(reference["expected_size"] is None or reference["expected_size"] == actual["size"], "backup_object_mismatch")
                        _require(reference["expected_sha256"] is None or reference["expected_sha256"] == actual["sha256"], "backup_object_mismatch")
                        os.chmod(target, 0o600)
                        total += actual["size"]
                        _require(total <= MAX_TOTAL_BYTES, "backup_limit_exceeded")
                        status, reason = "present", None
                    objects.append({"key": reference["key"], "path": relative,
                                    "mandatory": reference["mandatory"], "references": reference["references"],
                                    "status": status, "reason": reason,
                                    **{key: captured.get(key) for key in ("size", "sha256", "version_id", "etag")}})
        manifest = {"format_version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "schema_revision": _revision(), "postgres_major": checked["pg_major"], "scope": SCOPE,
                    "source_storage": "oss" if isinstance(storage, OSSBackupStorage) else "local",
                    "tables": tables, "objects": objects, "dump": dump}
        encoded = _json(manifest)
        _require(len(encoded) <= MAX_MANIFEST_BYTES and total + len(encoded) <= MAX_TOTAL_BYTES, "backup_limit_exceeded")
        with (stage / "manifest.json").open("xb") as stream:
            os.chmod(stage / "manifest.json", 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        verify_site_backup(stage)
        _require(not destination.exists(), "backup_destination_exists")
        stage.rename(destination)
        stage = None
        return _summary(manifest)
    except SiteBackupError as error:
        if error.code in {"site_object_size_exceeded", "site_postgres_size_exceeded"}:
            raise SiteBackupError("backup_limit_exceeded") from None
        raise
    except (OSError, ValueError, TypeError, SQLAlchemyError):
        raise SiteBackupError("backup_failed") from None
    finally:
        engine.dispose()
        if stage is not None:
            # Only the unique staging directory created by this invocation.
            _no_aliases(stage)
            shutil.rmtree(stage)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _load_manifest(root):
    manifest_path = root / "manifest.json"
    _hash_file(manifest_path, MAX_MANIFEST_BYTES)
    with _regular_open(manifest_path) as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    _require(len(raw) <= MAX_MANIFEST_BYTES, "backup_limit_exceeded")
    manifest = json.loads(raw, object_pairs_hook=_pairs)
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_FIELDS)
    _require(type(manifest["format_version"]) is int and manifest["format_version"] == FORMAT_VERSION)
    _require(manifest["scope"] == SCOPE and manifest["schema_revision"] == _revision())
    _require(manifest["source_storage"] in {"local", "oss"})
    _require(_integer(manifest["postgres_major"], 16, minimum=16))
    _require(isinstance(manifest["created_at"], str) and len(manifest["created_at"]) <= 64)
    _require(datetime.fromisoformat(manifest["created_at"]).tzinfo is not None)
    tables = manifest["tables"]
    _require(isinstance(tables, dict) and set(tables) == TABLE_NAMES)
    for item in tables.values():
        _require(isinstance(item, dict) and set(item) == {"count", "sha256"}
                 and _integer(item["count"], MAX_ROWS)
                 and isinstance(item["sha256"], str) and HEX.fullmatch(item["sha256"]))
    _require(sum(item["count"] for item in tables.values()) <= MAX_ROWS, "backup_limit_exceeded")
    return manifest, len(raw)


def verify_site_backup(path):
    try:
        root = _path(path)
        _require(root.is_dir() and (root / "objects").is_dir())
        manifest, total = _load_manifest(root)
        dump = manifest["dump"]
        _require(isinstance(dump, dict) and set(dump) == {"path", "size", "sha256"}
                 and dump["path"] == "database.dump" and _integer(dump["size"], MAX_DUMP_BYTES, minimum=1)
                 and isinstance(dump["sha256"], str) and HEX.fullmatch(dump["sha256"]))
        _require(_hash_file(root / "database.dump", MAX_DUMP_BYTES) == {key: dump[key] for key in ("size", "sha256")})
        total += dump["size"]
        objects = manifest["objects"]
        _require(isinstance(objects, list) and len(objects) <= MAX_OBJECTS, "backup_limit_exceeded")
        keys, expected = set(), {"manifest.json", "database.dump", "objects"}
        for item in objects:
            _require(isinstance(item, dict) and set(item) == OBJECT_FIELDS)
            key = _key(item["key"])
            relative = "objects/" + hashlib.sha256(key.encode("utf-8")).hexdigest()
            _require(key not in keys and item["path"] == relative and type(item["mandatory"]) is bool)
            keys.add(key)
            refs = item["references"]
            _require(isinstance(refs, list) and 0 < len(refs) <= MAX_ROWS
                     and all(isinstance(ref, str) and len(ref) <= 160 and re.fullmatch(r"(?:files|file_variants|ai_runs):[0-9a-f-]{36}(?::pages:[0-9]+)?", ref) for ref in refs)
                     and len(set(refs)) == len(refs))
            _require(all(item[field] is None or isinstance(item[field], str) and 0 < len(item[field]) <= 1024
                         for field in ("version_id", "etag")))
            if item["status"] == "missing":
                _require(not item["mandatory"] and item["reason"] == "not_found_optional"
                         and all(item[field] is None for field in ("size", "sha256", "version_id", "etag")))
            else:
                _require(item["status"] == "present" and item["reason"] is None
                         and _integer(item["size"], MAX_OBJECT_BYTES, minimum=1)
                         and isinstance(item["sha256"], str) and HEX.fullmatch(item["sha256"]))
                _require(_hash_file(root / relative, MAX_OBJECT_BYTES) == {key: item[key] for key in ("size", "sha256")})
                total += item["size"]
                expected.add(relative)
            _require(total <= MAX_TOTAL_BYTES, "backup_limit_exceeded")
        actual = set()
        for parent, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                entry = Path(parent) / name
                _no_aliases(entry)
                actual.add(entry.relative_to(root).as_posix())
                _require(len(actual) <= MAX_OBJECTS + 3, "backup_limit_exceeded")
        _require(actual == expected)
        return manifest
    except SiteBackupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise SiteBackupError("backup_invalid") from None


@contextmanager
def _snapshot(path):
    source = _path(path)
    manifest = verify_site_backup(source)
    with tempfile.TemporaryDirectory(prefix="ai-notes-site-restore-") as name:
        root = Path(name)
        os.chmod(root, 0o700)
        (root / "objects").mkdir(mode=0o700)
        paths = ["manifest.json", "database.dump"] + [item["path"] for item in manifest["objects"] if item["status"] == "present"]
        total = 0
        for relative in paths:
            limit = MAX_MANIFEST_BYTES if relative == "manifest.json" else MAX_DUMP_BYTES if relative == "database.dump" else MAX_OBJECT_BYTES
            copied = 0
            with _regular_open(source / relative) as original, (root / relative).open("xb") as target:
                os.chmod(root / relative, 0o600)
                while chunk := original.read(CHUNK_BYTES):
                    total += len(chunk)
                    copied += len(chunk)
                    _require(copied <= limit and total <= MAX_TOTAL_BYTES, "backup_limit_exceeded")
                    target.write(chunk)
        verified = verify_site_backup(root)
        _require(verified == manifest)
        yield root, verified


@contextmanager
def _storage_lock(storage_root, backup_root):
    root = _path(storage_root)
    _separate(root, _path(backup_root), "restore_storage_invalid")
    protected = {REPOSITORY / ".local" / "files", REPOSITORY / "backend" / ".local" / "files"}
    # Protect configured development storage too, without using its DB target.
    from app.shared.config import settings
    configured = Path(settings().storage_root)
    protected.update({configured.absolute(), REPOSITORY / configured, REPOSITORY / "backend" / configured})
    for item in protected:
        _separate(root, item, "restore_storage_invalid")
    _require(not root.exists() or root.is_dir(), "restore_storage_invalid")
    root.mkdir(parents=True, exist_ok=True)
    _no_aliases(root)
    _require(not any(root.iterdir()), "restore_storage_not_empty")
    os.chmod(root, 0o700)
    marker = root / ".site-restore-lock"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SiteBackupError("restore_locked") from None
    os.close(descriptor)
    try:
        _require(set(root.iterdir()) == {marker}, "restore_storage_not_empty")
        yield root
    finally:
        marker.unlink(missing_ok=True)


def _normalize(connection):
    commands = {
        "sessions": "UPDATE public.sessions SET revoked_at=now() WHERE revoked_at IS NULL",
        "file_tickets": "DELETE FROM public.file_tickets",
        "password_resets": "DELETE FROM public.password_resets",
        "ai_credentials": "DELETE FROM public.ai_credentials",
        "jobs": "UPDATE public.jobs SET status='failed',error_code='site_restore_interrupted',lease_until=NULL,worker_token=NULL WHERE status IN ('queued','running')",
        "ai_runs": "UPDATE public.ai_runs SET status='interrupted',error_code='site_restore_interrupted',lease_until=NULL,finished_at=now() WHERE status IN ('queued','running')",
        "ai_steps": "UPDATE public.ai_steps SET status='failed',error_code='site_restore_interrupted' WHERE status='running'",
    }
    return {table: connection.execute(text(statement)).rowcount for table, statement in commands.items()}


def _verify_restored_references(connection, manifest):
    references = {item["key"]: item for item in _references(connection)}
    objects = {item["key"]: item for item in manifest["objects"]}
    _require(references.keys() == objects.keys(), "restore_verification_failed")
    for key, reference in references.items():
        item = objects[key]
        _require(reference["mandatory"] == item["mandatory"]
                 and sorted(reference["references"]) == sorted(item["references"]), "restore_verification_failed")
        if item["status"] == "present":
            _require(reference["expected_size"] is None or reference["expected_size"] == item["size"], "restore_verification_failed")
            _require(reference["expected_sha256"] is None or reference["expected_sha256"] == item["sha256"], "restore_verification_failed")


def restore_site_backup(path, database_url, postgres_container, storage_root, *, isolated_confirmed=False):
    _require(isolated_confirmed is True, "restore_isolation_required")
    engine = _engine(database_url, restore=True)
    attempted = False
    try:
        with _snapshot(path) as (snapshot, manifest):
            postgres = DockerPostgres(engine, postgres_container)
            checked = postgres.check()
            _require(checked["pg_major"] == manifest["postgres_major"], "restore_postgres_version_mismatch")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
                locked = lock.execute(text("SELECT pg_try_advisory_lock(716493812, 214061)" )).scalar_one()
                _require(locked, "restore_locked")
                try:
                    _schema(lock, empty=True)
                    with _storage_lock(storage_root, path) as root:
                        try:
                            attempted = True
                            for item in manifest["objects"]:
                                if item["status"] == "missing":
                                    continue
                                target = root.joinpath(*item["key"].split("/"))
                                _no_aliases(target)
                                target.parent.mkdir(parents=True, exist_ok=True)
                                _no_aliases(target.parent)
                                with (snapshot / item["path"]).open("rb") as original, target.open("xb") as output:
                                    os.chmod(target, 0o600)
                                    shutil.copyfileobj(original, output, CHUNK_BYTES)
                                _require(_hash_file(target, MAX_OBJECT_BYTES) == {key: item[key] for key in ("size", "sha256")}, "restore_verification_failed")
                            # Recheck immediately before the external restore transaction.
                            _schema(lock, empty=True)
                            postgres.restore(snapshot / "database.dump")
                            with engine.begin() as connection:
                                metadata = _schema(connection)
                                _require(_fingerprints(connection, metadata) == manifest["tables"], "restore_verification_failed")
                                _verify_restored_references(connection, manifest)
                                normalization = _normalize(connection)
                            return {**_summary(manifest), "original_fingerprints_verified": True,
                                    "normalization": normalization}
                        except BaseException:
                            (root / ".site-restore-incomplete").write_text(
                                "Isolated restore incomplete. Preserve database and objects for operator review; do not retry over them.\n", encoding="utf-8")
                            os.chmod(root / ".site-restore-incomplete", 0o600)
                            raise
                finally:
                    lock.execute(text("SELECT pg_advisory_unlock(716493812, 214061)"))
    except SiteBackupError:
        raise
    except (OSError, ValueError, TypeError, SQLAlchemyError):
        raise SiteBackupError("restore_incomplete" if attempted else "restore_failed") from None
    finally:
        engine.dispose()


def _database_url_file(path):
    value = Path(path).read_text(encoding="utf-8-sig")
    _require(len(value) <= 8192 and len(value.splitlines()) == 1, "database_url_file_invalid")
    return value.strip()


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database-url-file", required=True)
    backup.add_argument("--postgres-container", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--storage-backend", choices=("local", "oss"), required=True)
    backup.add_argument("--storage-root")
    backup.add_argument("--env-file")
    backup.add_argument("--maintenance-confirmed", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("path")
    restore = commands.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--database-url-file", required=True)
    restore.add_argument("--postgres-container", required=True)
    restore.add_argument("--storage-root", required=True)
    restore.add_argument("--isolated-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = _summary(verify_site_backup(args.path))
        elif args.command == "restore":
            result = restore_site_backup(args.path, _database_url_file(args.database_url_file),
                                         args.postgres_container, args.storage_root,
                                         isolated_confirmed=args.isolated_confirmed)
        else:
            if args.storage_backend == "local":
                _require(bool(args.storage_root) and args.env_file is None, "backup_storage_config_invalid")
                storage = LocalBackupStorage(args.storage_root)
            else:
                _require(bool(args.env_file) and args.storage_root is None, "backup_storage_config_invalid")
                from dotenv import dotenv_values
                from app.shared.config import Settings
                values = dotenv_values(args.env_file, interpolate=False)
                defaults = {"oss_region": "cn-hangzhou", "oss_bucket": "", "oss_endpoint": "",
                            "oss_prefix": "ai-notes/", "oss_access_key_id": "",
                            "oss_access_key_secret": "", "oss_security_token": ""}
                cfg = Settings(_env_file=None, app_env="development", cookie_secure=False,
                               database_url="postgresql+psycopg://notes:@127.0.0.1:5432/notes",
                               storage_backend="oss", deepseek_api_key="",
                               **{key: values.get(key.upper()) or default for key, default in defaults.items()})
                storage = OSSBackupStorage(cfg)
            result = backup_site(_database_url_file(args.database_url_file), args.postgres_container,
                                 args.output, storage, maintenance_confirmed=args.maintenance_confirmed)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except SiteBackupError as error:
        print(json.dumps({"ok": False, "error": error.code}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        # Configuration exceptions may include secrets in their normal repr.
        print('{"ok":false,"error":"configuration_invalid"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
