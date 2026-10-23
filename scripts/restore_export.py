"""Restore one account archive into an explicitly selected, empty local sandbox.

This is an offline operator tool in the complete repository, not a web importer.
It never reads the application's configured database URL to select its target,
starts workers, restores credentials, or resumes model/processing requests.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from uuid import UUID, uuid4
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "backend"))

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, Uuid, create_engine, insert, inspect, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.modules.ai.schemas import RunOut  # noqa: E402
from app.modules.ai.source_context import public_source  # noqa: E402
from app.modules.exports.service import FIELDS  # noqa: E402
from app.modules.files.models import UploadSession  # noqa: E402
from app.modules.files.storage import LocalStorage  # noqa: E402
from app.modules.notes.models import Folder  # noqa: E402
from app.modules.notes.schemas import validate_body, validate_pg_text  # noqa: E402
from app.shared.db import Base  # noqa: E402
from app.shared.config import settings  # noqa: E402
from app.shared.models import Job, User  # noqa: E402
from app.shared.security import hasher  # noqa: E402


MAX_ARCHIVE_BYTES = 528 * 1024 * 1024
MAX_OBJECT_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
MODELS = {model.__tablename__: model for model in FIELDS}
EXTRA_FIELDS = {
    "files": {"original_path", "original_unavailable_reason", "processing_status", "processing_error_code"},
    "file_variants": {"path"},
}
TERMINAL_RUNS = {"succeeded", "cancelled", "interrupted", "failed", "truncated"}
MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


class RestoreError(ValueError):
    """A short error code; never contain database URLs or archive content."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code="restore_schema_invalid"):
    if not condition:
        raise RestoreError(code)


def _target_url(database_url):
    try:
        url = make_url(database_url)
        _require(url.drivername == "postgresql+psycopg"
                 and url.host in {"localhost", "127.0.0.1", "::1"}
                 and re.fullmatch(r"notes_restore(?:_[a-z0-9_]+)?", url.database or "")
                 and not url.query, "restore_target_invalid")
        # Driver query parameters could replace the checked host/database. No
        # libpq environment variable is allowed to supply a missing target.
        _require(url.port is not None and 1 <= url.port <= 65535
                 and bool(url.username), "restore_target_invalid")
        return url
    except RestoreError:
        raise
    except (TypeError, ValueError, SQLAlchemyError):
        raise RestoreError("restore_target_invalid") from None


def _password(password):
    _require(isinstance(password, str) and 10 <= len(password) <= 128,
             "restore_password_invalid")
    try:
        validate_pg_text(password)
    except (ValueError, UnicodeError):
        raise RestoreError("restore_password_invalid") from None


def _no_aliases(path):
    """Reject symlinks and Windows junction/reparse aliases at every level."""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "restore_storage_invalid")


@contextmanager
def _storage_lock(storage_root):
    try:
        raw = Path(storage_root)
        _require(".." not in raw.parts, "restore_storage_invalid")
        root = raw.absolute()
        _no_aliases(root)
        configured = Path(settings().storage_root)
        protected = {REPOSITORY / ".local" / "files", REPOSITORY / "backend" / ".local" / "files"}
        protected.update({configured.absolute(), REPOSITORY / configured, REPOSITORY / "backend" / configured})
        resolved = root.resolve()
        _require(all(not resolved.is_relative_to(item.resolve()) and not item.resolve().is_relative_to(resolved)
                     for item in protected), "restore_storage_invalid")
        _require(not root.exists() or root.is_dir(), "restore_storage_invalid")
        root.mkdir(parents=True, exist_ok=True)
        _no_aliases(root)
        marker = root / ".restore-lock"
        # An existing marker is never removed, including a crashed earlier run.
        if marker.exists():
            raise RestoreError("restore_locked")
        _require(not any(root.iterdir()), "restore_storage_not_empty")
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise RestoreError("restore_locked") from None
        os.close(descriptor)
        try:
            _require(set(root.iterdir()) == {marker}, "restore_storage_not_empty")
            yield root.resolve()
        finally:
            marker.unlink(missing_ok=True)
    except RestoreError:
        raise
    except (OSError, ValueError, TypeError):
        raise RestoreError("restore_storage_unavailable") from None


def _load_verifier():
    spec = importlib.util.spec_from_file_location("ai_notes_export_verifier", REPOSITORY / "scripts" / "verify_export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _snapshot(archive_path):
    """Verify and consume the same private bounded copy, avoiding path races."""
    with tempfile.TemporaryDirectory(prefix="ai-notes-restore-") as directory:
        try:
            os.chmod(directory, 0o700)
            snapshot = Path(directory) / "archive.zip"
            copied = 0
            with Path(archive_path).open("rb") as source, snapshot.open("xb") as target:
                while chunk := source.read(CHUNK_BYTES):
                    copied += len(chunk)
                    _require(copied <= MAX_ARCHIVE_BYTES, "restore_archive_invalid")
                    target.write(chunk)
            verifier = _load_verifier()
            try:
                counts = verifier.verify_archive(snapshot)
            except verifier.ValidationError:
                raise RestoreError("restore_archive_invalid") from None
            archive = zipfile.ZipFile(snapshot)
            try:
                manifest = verifier._metadata(archive, archive.getinfo("manifest.json"))
                data = verifier._metadata(archive, archive.getinfo("data.json"))
            except BaseException:
                archive.close()
                raise
        except RestoreError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile):
            raise RestoreError("restore_archive_invalid") from None
        with archive:
            yield archive, manifest, data["collections"], counts


def _json_safe(value):
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        _require(depth <= 128)
        if isinstance(item, dict):
            for key, child in item.items():
                _require(isinstance(key, str))
                validate_pg_text(key)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            validate_pg_text(item)
        elif isinstance(item, float):
            _require(float("-inf") < item < float("inf"))
        else:
            _require(item is None or type(item) in {int, bool})


def _column_value(column, value):
    if value is None:
        _require(column.nullable)
        return None
    if isinstance(column.type, Uuid):
        _require(isinstance(value, str) and str(UUID(value)) == value)
        return UUID(value)
    if isinstance(column.type, DateTime):
        _require(isinstance(value, str))
        result = datetime.fromisoformat(value)
        _require(result.utcoffset() is not None)
        return result
    if isinstance(column.type, (Integer, BigInteger)):
        limit = 2 ** (63 if isinstance(column.type, BigInteger) else 31)
        _require(type(value) is int and -limit <= value < limit)
    elif isinstance(column.type, (String, Text)):
        _require(isinstance(value, str) and (not column.type.length or len(value) <= column.type.length))
        validate_pg_text(value)
    elif isinstance(column.type, JSONB):
        _json_safe(value)
    else:
        raise RestoreError("restore_schema_invalid")
    return value


def _positive(value, *, zero=False):
    _require(type(value) is int and value >= (0 if zero else 1))


def _unique(rows, fields):
    values = [tuple(row[field] for field in fields) for row in rows]
    _require(len(set(values)) == len(values))


def _source(source, *, internal, notes, pages, files, variants, keys, pdf_imports):
    if source is None:
        return None
    _require(isinstance(source, dict) and set(source) == {"kind", "note_id", "title", "revision", "mode", "pages"})
    _require(source["kind"] in {"pdf", "image"} and type(source["revision"]) is int
             and source["revision"] > 0 and isinstance(source["title"], str)
             and bool(source["title"].strip()) and len(source["title"]) <= 200)
    _require(notes[source["note_id"]]["kind"] == "image")
    selected = source["pages"]
    _require(isinstance(selected, list) and 0 < len(selected) <= 200)
    image = source["kind"] == "image"
    _require(source["mode"] in ({"page", "region"} if image else {"pages", "all"}))
    if image:
        _require(len(selected) == 1 and (source["mode"] == "region") == (selected[0].get("region") is not None))
    else:
        item = pdf_imports[source["note_id"]]
        _require(item["page_count"] is not None)
    seen_ids, seen_numbers = set(), set()
    for frozen in selected:
        expected_fields = {"page_id", "number", "file_id", "reading_path"} | ({"region"} if image else set())
        _require(isinstance(frozen, dict) and set(frozen) == expected_fields)
        _positive(frozen["number"])
        _require(frozen["page_id"] not in seen_ids and frozen["number"] not in seen_numbers)
        seen_ids.add(frozen["page_id"])
        seen_numbers.add(frozen["number"])
        actual = pages[frozen["page_id"]]
        file = files[frozen["file_id"]]
        variant = variants[(frozen["file_id"], "reading")]
        _require(str(actual["note_id"]) == source["note_id"] and actual["file_id"] == file["id"]
                 and file["status"] == "ready" and file["mime"] != "application/pdf"
                 and frozen["reading_path"] == f"objects/{file['id']}/variants/{variant['id']}")
        _require(actual["pdf_page_number"] is None if image else actual["pdf_page_number"] == frozen["number"])
        _require(variant["bytes"] <= 8 * 1024 * 1024)
        if not image:
            _require(frozen["number"] <= item["page_count"])
    if not image and source["mode"] == "all":
        # selected is bounded to 200 before constructing this range. A damaged
        # processing import's large page_count must never cause an allocation.
        _require(len(selected) == item["page_count"]
                 and seen_numbers == set(range(1, item["page_count"] + 1)))
    # The schemas validate bounded finite crop coordinates and kind/mode values.
    safe = public_source(source).model_dump(mode="json")
    if not internal:
        return safe
    for frozen, original in zip(safe["pages"], selected, strict=True):
        variant = variants[(original["file_id"], "reading")]
        frozen.update(file_id=original["file_id"], object_key=keys[original["reading_path"]], bytes=variant["bytes"])
    return safe


def _prepare(manifest, collections, prefix):
    """Strictly validate export-only fields, then build detached insert rows."""
    try:
        _require(set(manifest) == {"format", "version", "exported_at", "owner", "counts", "resume_processing", "payloads"})
        owner = manifest["owner"]
        _require(set(owner) == {"id", "username", "display_name"})
        _require(isinstance(owner["username"], str) and re.fullmatch(r"[a-zA-Z0-9_-]{3,32}", owner["username"]))
        _require(isinstance(owner["display_name"], str) and 1 <= len(owner["display_name"]) <= 64
                 and bool(owner["display_name"].strip()))
        _json_safe(owner)
        _require(all(set(item) == {"path", "bytes", "sha256"} for item in manifest["payloads"]))
        # This first recovery tool covers the application's default 50 MiB
        # upload limit. Refuse a larger object before allocating its contents.
        _require(all(item["path"] == "data.json" or item["bytes"] <= MAX_OBJECT_BYTES
                     for item in manifest["payloads"]), "restore_object_too_large")
        rows, indexes, keys = {}, {}, {}
        for table, model in MODELS.items():
            required = set(FIELDS[model])
            if table == "files":
                required.add("original_path")
            elif table == "file_variants":
                required.add("path")
            rows[table] = []
            for record in collections[table]:
                _require(required <= set(record) <= required | EXTRA_FIELDS.get(table, set()))
                row = {name: _column_value(model.__table__.c[name], record[name]) for name in FIELDS[model]}
                if "revision" in row:
                    _positive(row["revision"])
                for field in ("name", "title", "filename"):
                    if field in row:
                        _require(bool(row[field].strip()))
                rows[table].append(row)
            if table != "note_tags":
                indexes[table] = {str(row["id"]): row for row in rows[table]}
        _unique(rows["tags"], ("name",))
        _unique(rows["note_versions"], ("note_id", "revision"))
        _unique(rows["note_pages"], ("note_id", "file_id"))
        _unique([row for row in rows["note_pages"] if row["pdf_page_number"] is not None], ("note_id", "pdf_page_number"))
        _unique(rows["pdf_imports"], ("note_id",))
        _unique(rows["pdf_imports"], ("source_file_id",))
        _unique(rows["ai_steps"], ("run_id", "position"))
        _unique(rows["ai_saved_notes"], ("run_id",))
        _unique(rows["ai_saved_notes"], ("note_id",))
        for row in rows["notes"]:
            _require(row["kind"] in {"text", "image"} and isinstance(row["body_json"], dict))
            validate_body(row["body_json"], row["plain_text"])
            _require(row["kind"] != "image" or (row["body_json"] == {} and row["plain_text"] == ""))
        for row in rows["note_versions"]:
            # Snapshots are immutable historical JSON, including removed
            # organization IDs; never invent rows or rewrite that history.
            _require(isinstance(row["snapshot"], dict))
        files = indexes["files"]
        for row, original in zip(rows["files"], collections["files"], strict=True):
            _require(row["mime"] in MIMES and row["status"] in {"processing", "ready", "rejected", "expired"})
            _positive(row["expected_bytes"])
            if row["bytes"] is not None:
                _positive(row["bytes"])
            for field in ("width", "height"):
                if row[field] is not None:
                    _positive(row[field])
            if row["sha256"] is not None:
                _require(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is not None)
            unavailable = original.get("original_unavailable_reason")
            _require((original["original_path"] is None) == (unavailable is not None))
            if original["original_path"] is None:
                _require(row["status"] == "expired" and unavailable == "upload_expired"
                         or row["status"] == "rejected" and row["mime"] != "application/pdf" and unavailable == "image_rejected")
            else:
                _require(row["bytes"] == row["expected_bytes"])
            if "processing_status" in original or "processing_error_code" in original:
                _require(row["status"] == "processing" and row["mime"] != "application/pdf"
                         and original.get("processing_status") == "failed"
                         and "processing_error_code" in original)
                code = original["processing_error_code"]
                _require(code is None or isinstance(code, str) and len(code) <= 64)
                if code is not None:
                    validate_pg_text(code)
            # One archive has one owner. UUID hex keeps generated private paths
            # short enough for Windows temp-directory roots without filenames.
            row["object_key"] = f"{prefix}/{row['id'].hex}/original"
            if original["original_path"]:
                keys[original["original_path"]] = row["object_key"]
        variants, variant_kinds = {}, {}
        for row, original in zip(rows["file_variants"], collections["file_variants"], strict=True):
            _positive(row["bytes"])
            _require(row["kind"] in {"reading", "thumbnail"} and files[str(row["file_id"])]["mime"] != "application/pdf")
            row["object_key"] = f"{prefix}/{row['file_id'].hex}/v_{row['id'].hex}"
            keys[original["path"]] = row["object_key"]
            variants[(str(row["file_id"]), row["kind"])] = row
            variant_kinds.setdefault(row["file_id"], set()).add(row["kind"])
        for row in rows["files"]:
            if row["status"] == "ready" and row["mime"] != "application/pdf":
                _require(variant_kinds.get(row["id"]) == {"reading", "thumbnail"})
        notes, pages = indexes["notes"], indexes["note_pages"]
        pdf_by_note = {str(row["note_id"]): row for row in rows["pdf_imports"]}
        pdf_pages = {}
        for page in rows["note_pages"]:
            _positive(page["position"], zero=True)
            _require(notes[str(page["note_id"])]["kind"] == "image" and files[str(page["file_id"])]["mime"] != "application/pdf")
            if page["pdf_page_number"] is not None:
                _positive(page["pdf_page_number"])
                _require(str(page["note_id"]) in pdf_by_note)
                pdf_pages.setdefault(page["note_id"], []).append(page)
        for row in rows["pdf_imports"]:
            source = files[str(row["source_file_id"])]
            _require(notes[str(row["note_id"])]["kind"] == "image" and source["mime"] == "application/pdf")
            _require(row["status"] in {"uploading", "queued", "processing", "ready", "rejected"})
            _positive(row["completed_pages"], zero=True)
            if row["page_count"] is not None:
                _positive(row["page_count"])
                _require(row["completed_pages"] <= row["page_count"])
            actual = pdf_pages.get(row["note_id"], [])
            _require(len(actual) == row["completed_pages"])
            if row["status"] == "uploading":
                # Upload reservations without their original are expired;
                # received bytes are retained but no processing is resumed.
                _require(source["status"] == "expired" and row["completed_pages"] == 0)
            else:
                _require(source["status"] == "ready")
            if row["status"] == "ready":
                _require(row["page_count"] is not None and row["completed_pages"] == row["page_count"]
                         and sorted(page["pdf_page_number"] for page in actual) == list(range(1, row["page_count"] + 1))
                         and all(files[str(page["file_id"])]["status"] == "ready" for page in actual))
            row["request_hash"] = hashlib.sha256(uuid4().bytes).hexdigest()
        for row in rows["ai_conversations"]:
            row["request_key"] = uuid4()
        for row in rows["ai_runs"]:
            _require(row["status"] in TERMINAL_RUNS and isinstance(row["usage"], (dict, type(None)))
                     and isinstance(row["progress"], (dict, type(None))) and bool(row["prompt"].strip()))
            row.update(request_key=uuid4(), request_hash=hashlib.sha256(uuid4().bytes).hexdigest(), input_messages=[], lease_until=None)
            row["source"] = _source(row["source"], internal=True, notes=notes, pages=pages, files=files, variants=variants, keys=keys, pdf_imports=pdf_by_note)
            # API consumers must be able to read restored metadata, including
            # usage/progress. Do not normalize or rewrite its valid JSON.
            RunOut.model_validate(row)
        normalized_steps = 0
        for row in rows["ai_steps"]:
            _positive(row["position"], zero=True)
            _require(row["kind"] in {"read", "reduce", "answer"} and row["status"] in {"running", "succeeded", "failed"}
                     and isinstance(row["pages"], list) and isinstance(row["usage"], (dict, type(None))))
            for number in row["pages"]:
                _positive(number)
            _require(len(set(row["pages"])) == len(row["pages"]))
            if row["status"] == "running":
                row.update(status="failed", error_code="restore_not_resumed")
                normalized_steps += 1
        for row in rows["ai_saved_notes"]:
            _require(isinstance(row["cited_page_numbers"], list))
            for number in row["cited_page_numbers"]:
                _positive(number)
            _require(len(set(row["cited_page_numbers"])) == len(row["cited_page_numbers"]))
            row["source"] = _source(row["source"], internal=False, notes=notes, pages=pages, files=files, variants=variants, keys=keys, pdf_imports=pdf_by_note)
            original_run = indexes["ai_runs"][str(row["run_id"])]
            _require(notes[str(row["note_id"])]["kind"] == "text" and original_run["status"] == "succeeded"
                     and bool(original_run["answer"].strip())
                     and row["source"] == (public_source(original_run["source"]).model_dump(mode="json") if original_run["source"] else None))
            _require(not row["cited_page_numbers"] or row["source"] is not None
                     and set(row["cited_page_numbers"]) <= {page["number"] for page in row["source"]["pages"]})
        return rows, keys, normalized_steps
    except RestoreError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise RestoreError("restore_schema_invalid") from None


def _synthesized(rows, collections, owner_id):
    now = datetime.now(timezone.utc)
    uploads, jobs = [], []
    original_files = {row["id"]: row for row in collections["files"]}
    for item in rows["pdf_imports"]:
        available = original_files[str(item["source_file_id"])]["original_path"] is not None
        uploads.append({"id": uuid4(), "owner_id": owner_id, "created_at": now,
                        "file_id": item["source_file_id"], "note_id": item["note_id"],
                        "request_key": str(uuid4()), "status": "completed" if available else "expired",
                        "expires_at": now - timedelta(seconds=1)})
        if item["status"] in {"queued", "processing"}:
            jobs.append({"id": uuid4(), "created_at": now, "owner_id": owner_id,
                         "kind": "pdf.import", "status": "failed", "dedupe_key": f"pdf_{item['id']}",
                         "payload": {"import_id": str(item["id"])}, "error_code": item["error_code"] or "restore_processing_not_resumed"})
    for item in rows["files"]:
        if item["status"] == "processing" and item["mime"] != "application/pdf":
            original = original_files[str(item["id"])]
            jobs.append({"id": uuid4(), "created_at": now, "owner_id": owner_id,
                         "kind": "image.process", "status": "failed", "dedupe_key": f"image_{item['id']}",
                         "payload": {"file_id": str(item["id"])},
                         "error_code": original.get("processing_error_code") or item["error_code"] or "restore_processing_not_resumed"})
    return uploads, jobs


def _migration_head():
    cfg = Config()
    cfg.set_main_option("script_location", str(REPOSITORY / "backend" / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    _require(len(heads) == 1, "restore_migration_mismatch")
    return heads[0]


def _check_database(connection):
    # Lock before checking emptiness. NOWAIT fails closed if another process is
    # using an application table; inserts cannot race our empty-target check.
    preparer = connection.dialect.identifier_preparer
    schema = inspect(connection)
    expected_names = set(Base.metadata.tables) | {"alembic_version"}
    _require(set(schema.get_table_names(schema="public")) == expected_names, "restore_schema_invalid")
    names = ", ".join('"public".' + preparer.quote(name) for name in sorted(expected_names))
    connection.execute(text("LOCK TABLE " + names + " IN ACCESS EXCLUSIVE MODE NOWAIT"))
    version = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    _require(version == [_migration_head()], "restore_migration_mismatch")
    for table in Base.metadata.sorted_tables:
        columns = {column["name"]: column for column in schema.get_columns(table.name, schema="public")}
        _require(set(columns) == set(table.c.keys()), "restore_schema_invalid")
        for column in table.c:
            actual = columns[column.name]
            _require(actual["nullable"] == column.nullable
                     and str(actual["type"].compile(dialect=connection.dialect)) == str(column.type.compile(dialect=connection.dialect)),
                     "restore_schema_invalid")
        _require(connection.execute(select(table).limit(1)).first() is None, "restore_database_not_empty")


def _insert_rows(connection, model, rows):
    # Core multi-row inserts require identical column keys. All synthesized
    # rows have identical fields; chunks avoid the PostgreSQL bind limit.
    for start in range(0, len(rows), 100):
        connection.execute(insert(model.__table__), rows[start:start + 100])


def _write_objects(archive, manifest, keys, storage):
    try:
        for item in manifest["payloads"]:
            if item["path"] == "data.json":
                continue
            _require(item["bytes"] <= MAX_OBJECT_BYTES, "restore_object_too_large")
            # The snapshot was verified; recheck the exact bytes being written as a
            # second barrier rather than trusting an extraction or user filename.
            with archive.open(item["path"]) as stream:
                data = stream.read(item["bytes"] + 1)
            _require(len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"], "restore_archive_invalid")
            storage.put(keys[item["path"]], data)
            del data
            actual = storage.read(keys[item["path"]])
            _require(len(actual) == item["bytes"] and hashlib.sha256(actual).hexdigest() == item["sha256"], "restore_storage_unavailable")
            del actual
    except RestoreError:
        raise
    except (OSError, ValueError):
        raise RestoreError("restore_storage_unavailable") from None


def _folder_order(rows):
    # Breadth-first topological order is linear even for a deeply nested tree.
    children, ordered = {}, []
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    ordered.extend(children.get(None, []))
    cursor = 0
    while cursor < len(ordered):
        ordered.extend(children.get(ordered[cursor]["id"], []))
        cursor += 1
    _require(len(ordered) == len(rows))
    return ordered


def _cleanup_prefix(root, prefix):
    # This is the only recursive deletion: a random prefix made exclusively by
    # this invocation, after a known pre-commit failure. Never delete the root.
    _require(re.fullmatch(r"restore_[0-9a-f]{32}", prefix) is not None, "restore_storage_invalid")
    _no_aliases(root)
    target = root / prefix
    if target.exists():
        _no_aliases(target)
        _require(target.resolve().parent == root.resolve() and target.resolve().is_relative_to(root.resolve()), "restore_storage_invalid")
        for item in target.rglob("*"):
            _no_aliases(item)
        shutil.rmtree(target)


def _commit(connection):
    """Separate commit boundary for testing uncertain acknowledgement outcomes."""
    connection.commit()


def restore_archive(archive_path, *, database_url, storage_root, password):
    """Restore a verified archive once, without touching any nonempty target."""
    url = _target_url(database_url)
    _password(password)
    prefix = "restore_" + uuid4().hex
    engine = None
    commit_attempted = False
    try:
        with _snapshot(archive_path) as (archive, manifest, collections, counts):
            rows, keys, normalized_steps = _prepare(manifest, collections, prefix)
            owner_id = UUID(manifest["owner"]["id"])
            uploads, jobs = _synthesized(rows, collections, owner_id)
            password_hash = hasher.hash(password)
            with _storage_lock(storage_root) as root:
                try:
                    # Explicit target only. Importing application metadata does
                    # not connect through app.shared.db.engine or SessionLocal.
                    engine = create_engine(url, pool_pre_ping=True, connect_args={
                        "connect_timeout": 5, "hostaddr": "::1" if url.host == "::1" else "127.0.0.1",
                        "options": "-csearch_path=public",
                    }, hide_parameters=True)
                    with engine.connect() as connection:
                        connection.begin()
                        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                        _check_database(connection)
                        connection.execute(insert(User.__table__).values(id=owner_id,
                            username=manifest["owner"]["username"], display_name=manifest["owner"]["display_name"],
                            password_hash=password_hash, role="user", status="active"))
                        # Folders have self-references. The verifier established
                        # acyclicity; insert each level after all its parents.
                        _insert_rows(connection, Folder, _folder_order(rows["folders"]))
                        for table, model in MODELS.items():
                            if table != "folders":
                                _insert_rows(connection, model, rows[table])
                        _insert_rows(connection, UploadSession, uploads)
                        _insert_rows(connection, Job, jobs)
                        _write_objects(archive, manifest, keys, LocalStorage(root))
                        commit_attempted = True
                        _commit(connection)
                except BaseException:
                    if not commit_attempted:
                        try:
                            _cleanup_prefix(root, prefix)
                        except (OSError, RestoreError):
                            raise RestoreError("restore_cleanup_incomplete") from None
                    else:
                        raise RestoreError("restore_commit_uncertain") from None
                    raise
            return {"owner_id": str(owner_id), "counts": counts, "normalized_steps": normalized_steps,
                    "synthesized_jobs": len(jobs), "objects": len(keys)}
    except RestoreError:
        raise
    except SQLAlchemyError:
        raise RestoreError("restore_database_unavailable") from None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise RestoreError("restore_unavailable") from None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="将账号导出包恢复到独立、空白的本地恢复库和文件目录。不会合并现有数据或启动处理任务。")
    parser.add_argument("archive", help="账号导出 ZIP")
    parser.add_argument("--database-url-env", default="RESTORE_DATABASE_URL", help="保存显式恢复库 URL 的环境变量名（默认 RESTORE_DATABASE_URL）")
    parser.add_argument("--storage-root", required=True, help="不存在或空白的独立文件目录")
    parser.add_argument("--password-file", help="新登录密码文件；省略时隐藏输入并确认，不接受明文命令行密码")
    arguments = parser.parse_args()
    try:
        database_url = os.environ.get(arguments.database_url_env)
        _target_url(database_url)
        if arguments.password_file:
            with Path(arguments.password_file).open("r", encoding="utf-8") as stream:
                password = stream.read(1024)
                _require(not stream.read(1), "restore_password_invalid")
            password = password.removesuffix("\n").removesuffix("\r")
        else:
            password = getpass.getpass("恢复账号的新密码（10 至 128 个字符）：")
            _require(password == getpass.getpass("再次输入新密码："), "restore_password_invalid")
        result = restore_archive(arguments.archive, database_url=database_url,
                                 storage_root=arguments.storage_root, password=password)
    except RestoreError as error:
        print(f"恢复失败：{error.code}", file=sys.stderr)
        return 1
    except (OSError, EOFError, KeyboardInterrupt):
        print("恢复失败：restore_input_unavailable", file=sys.stderr)
        return 1
    print(f"恢复成功：{len(result['counts'])} 类数据，共 {sum(result['counts'].values())} 条记录；"
          f"{result['objects']} 个文件对象，{result['normalized_steps']} 个未完成 AI 步骤已停止，"
          f"{result['synthesized_jobs']} 个处理任务保留为失败状态。未启动后台任务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
