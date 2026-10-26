"""Read-only, fail-closed reachability for private file garbage collection.

The caller owns the consistent database snapshot and maintenance exclusion.
Registered files are retention roots even when their notes are in the trash or
their upload/processing failed. This module neither reads storage nor deletes
database rows, and it never reads credentials or unrelated note bodies.
"""
import hashlib
import json
import math
import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


MAX_ROWS = 1_000_000
MAX_KEYS = 100_000
_KEY = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+\Z")


class RetentionError(ValueError):
    def __init__(self, code="retention_reference_invalid"):
        self.code = code
        super().__init__(code)


def _require(condition):
    if not condition:
        raise RetentionError()


def _uuid(value):
    _require(isinstance(value, (str, UUID)))
    result = str(UUID(str(value)))
    _require(str(value) == result)
    return result


def _positive(value, *, zero=False):
    _require(type(value) is int and value >= (0 if zero else 1))
    return value


def _key(value):
    _require(isinstance(value, str) and len(value) <= 256 and _KEY.fullmatch(value)
             and all(part not in {".", ".."} for part in value.split("/")))
    return value


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str).encode("utf-8")


def _region(value):
    _require(isinstance(value, dict) and set(value) == {"x", "y", "width", "height"})
    _require(all(type(number) in {int, float} and math.isfinite(number) for number in value.values()))
    _require(0 <= value["x"] < 1 and 0 <= value["y"] < 1
             and 0 < value["width"] <= 1 and 0 < value["height"] <= 1
             and value["x"] + value["width"] <= 1 and value["y"] + value["height"] <= 1)


def _canonical_text(body):
    # Keep the same pure helpers/rules as text_context without importing ORM
    # modules, which would initialize the application settings/engine.
    from app.modules.ai.answer_format import plain_text
    from app.modules.notes.schemas import validate_body

    _require(isinstance(body, dict))
    validate_body(body, "")
    stack = [body]
    while stack:
        node = stack.pop()
        _require(isinstance(node, dict))
        kind = node.get("type")
        _require(kind is None or isinstance(kind, str))
        if kind == "text":
            _require(isinstance(node.get("text"), str))
        if kind in {"inlineMath", "blockMath"}:
            attrs = node.get("attrs", {})
            _require(isinstance(attrs, dict) and isinstance(attrs.get("latex", ""), str))
        children = node.get("content", [])
        _require(isinstance(children, list))
        stack.extend(children)
    return plain_text(body)


def collect_references(connection):
    """Return keys/counts/active_writers/fingerprint, without exposing content.

    Visual frozen keys remain roots even if they differ from today's reading
    variant. Current access checks must not be used to decide retention: a
    deleted source or an old page identity can become readable after recovery.
    Unknown/damaged references abort the entire collection rather than turn
    unreadable data into apparently unreferenced objects.
    """
    try:
        return _collect(connection)
    except RetentionError:
        raise
    except SQLAlchemyError:
        raise RetentionError("retention_database_unavailable") from None
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError, UnicodeError, OverflowError):
        raise RetentionError() from None


def _collect(connection):
    counts, snapshot, total_rows = {}, {}, 0
    keys = set()
    key_owners = {}

    def rows(table, columns):
        nonlocal total_rows
        result = [dict(row) for row in connection.execute(text(
            f"SELECT {columns} FROM public.{table} ORDER BY id LIMIT {MAX_ROWS + 1}"
        )).mappings()]
        total_rows += len(result)
        if total_rows > MAX_ROWS:
            raise RetentionError("retention_limit_exceeded")
        counts[table] = len(result)
        snapshot[table] = result
        return result

    def keep(value):
        keys.add(_key(value))
        if len(keys) > MAX_KEYS:
            raise RetentionError("retention_limit_exceeded")

    notes = {_uuid(row["id"]): row for row in rows("notes", "id,owner_id,kind,revision,deleted_at")}
    for row in notes.values():
        row["owner_id"] = _uuid(row["owner_id"])
        _require(row["kind"] in {"text", "image"})
        _positive(row["revision"])

    def note(note_id, owner, kind=None):
        row = notes[_uuid(note_id)]
        _require(row["owner_id"] == owner and (kind is None or row["kind"] == kind))
        return row

    files = {_uuid(row["id"]): row for row in rows("files", "id,owner_id,object_key,mime,status")}
    for row in files.values():
        row["owner_id"] = _uuid(row["owner_id"])
        _require(row["status"] in {"uploading", "processing", "ready", "rejected", "expired"})
        _require(isinstance(row["mime"], str))
        keep(row["object_key"])
        key_owners.setdefault(row["object_key"], set()).add(row["owner_id"])

    def file(file_id, owner):
        row = files[_uuid(file_id)]
        _require(row["owner_id"] == owner)
        keep(row["object_key"])
        return row

    variants = rows("file_variants", "id,file_id,kind,object_key,bytes")
    for row in variants:
        _uuid(row["id"])
        _require(_uuid(row["file_id"]) in files and row["kind"] in {"reading", "thumbnail"})
        _positive(row["bytes"])
        keep(row["object_key"])
        key_owners.setdefault(row["object_key"], set()).add(files[_uuid(row["file_id"])]["owner_id"])
    _require(all(len(owners) == 1 for owners in key_owners.values()))

    pages = {_uuid(row["id"]): row for row in rows(
        "note_pages", "id,owner_id,note_id,file_id,position,revision,pdf_page_number")}
    historical_pages = {}
    for page_id, row in pages.items():
        owner = _uuid(row["owner_id"])
        note(row["note_id"], owner, "image")
        _require(file(row["file_id"], owner)["mime"] != "application/pdf")
        _positive(row["position"], zero=True)
        _positive(row["revision"])
        if row["pdf_page_number"] is not None:
            _positive(row["pdf_page_number"])
        historical_pages.setdefault(page_id, set()).add((owner, _uuid(row["note_id"]), _uuid(row["file_id"])))

    versions = {}
    for row in rows("note_versions", "id,owner_id,note_id,revision,jsonb_typeof(snapshot) AS snapshot_type,"
                    "snapshot->'id' AS snapshot_id,snapshot->'owner_id' AS snapshot_owner_id,"
                    "snapshot->'kind' AS snapshot_kind,snapshot->'revision' AS snapshot_revision,"
                    "snapshot->'pages' AS pages"):
        owner, note_id = _uuid(row["owner_id"]), _uuid(row["note_id"])
        parent = note(note_id, owner)
        revision = _positive(row["revision"])
        _require(row["snapshot_type"] == "object" and row["snapshot_id"] == note_id
                 and row["snapshot_owner_id"] == owner and row["snapshot_kind"] == parent["kind"]
                 and type(row["snapshot_revision"]) is int and row["snapshot_revision"] == revision)
        _require((note_id, revision) not in versions)
        versions[(note_id, revision)] = row
        selected = row["pages"]
        if parent["kind"] == "image":
            _require(isinstance(selected, list))
        if selected is not None:
            _require(isinstance(selected, list))
            seen = set()
            for page in selected:
                _require(isinstance(page, dict))
                page_id, file_id = _uuid(page["id"]), _uuid(page["file_id"])
                _require(page_id not in seen and file(file_id, owner)["mime"] != "application/pdf")
                seen.add(page_id)
                _positive(page["position"], zero=True)
                _positive(page["revision"])
                historical_pages.setdefault(page_id, set()).add((owner, note_id, file_id))

    uploads = rows("upload_sessions", "id,owner_id,file_id,note_id,status,expires_at")
    active_uploads = 0
    uploads_by_file = {}
    for row in uploads:
        owner = _uuid(row["owner_id"])
        note(row["note_id"], owner, "image")
        file(row["file_id"], owner)
        _require(row["status"] in {"pending", "received", "completed", "expired"})
        file_id = _uuid(row["file_id"])
        _require(file_id not in uploads_by_file)
        uploads_by_file[file_id] = row
        active_uploads += row["status"] in {"pending", "received"}

    imports = {_uuid(row["id"]): row for row in rows(
        "pdf_imports", "id,owner_id,note_id,source_file_id,status,page_count,completed_pages")}
    active_imports = 0
    imports_by_note = {}
    for row in imports.values():
        owner = _uuid(row["owner_id"])
        note(row["note_id"], owner, "image")
        _require(file(row["source_file_id"], owner)["mime"] == "application/pdf")
        _require(row["status"] in {"uploading", "queued", "processing", "ready", "rejected"})
        upload = uploads_by_file[_uuid(row["source_file_id"])]
        _require(_uuid(upload["owner_id"]) == owner and _uuid(upload["note_id"]) == _uuid(row["note_id"]))
        imports_by_note[_uuid(row["note_id"])] = row
        active_imports += (row["status"] in {"queued", "processing"}
                           or row["status"] == "uploading" and upload["status"] != "expired")

    conversations = {_uuid(row["id"]): _uuid(row["owner_id"]) for row in rows(
        "ai_conversations", "id,owner_id")}
    text_versions = {}

    def source(value, owner, *, internal):
        if value is None:
            return
        _require(isinstance(value, dict))
        kind = value.get("kind", "pdf")  # Pre-kind PDF snapshots remain supported.
        _require(kind in {"text", "image", "pdf"})
        if kind == "text":
            from app.modules.ai.text_schemas import TextSourceOut

            _require(set(value) == {"kind", "note_id", "title", "revision", "start", "end", "quote"})
            public = TextSourceOut.model_validate(value)
            note_id = str(public.note_id)
            note(note_id, owner, "text")
            version = versions[(note_id, public.revision)]
            identity = str(version["id"])
            if identity not in text_versions:
                # Fetch body only for the historical version actually cited.
                row = connection.execute(text(
                    "SELECT snapshot->'title' AS title,snapshot->'body_json' AS body_json "
                    "FROM public.note_versions WHERE id=:id"
                ), {"id": version["id"]}).mappings().one()
                text_versions[identity] = (row["title"], _canonical_text(row["body_json"]))
            title, original = text_versions[identity]
            _require(title == public.title and public.end <= len(original)
                     and original[public.start:public.end] == public.quote)
            return
        _require(set(value) <= {"kind", "note_id", "title", "revision", "mode", "pages"})
        note_id = _uuid(value["note_id"])
        note(note_id, owner, "image")
        if kind == "pdf":
            _require(note_id in imports_by_note)
        _positive(value["revision"])
        _require(isinstance(value["title"], str) and 0 < len(value["title"].strip()) <= 200)
        mode = value["mode"]
        _require(mode in ({"page", "region"} if kind == "image" else {"pages", "all", "region"}))
        selected = value["pages"]
        _require(isinstance(selected, list) and 0 < len(selected) <= 200
                 and (kind != "image" or len(selected) == 1)
                 and (mode != "region" or len(selected) == 1))
        seen_ids, seen_numbers = set(), set()
        for page in selected:
            _require(isinstance(page, dict))
            allowed = {"page_id", "number", "region"} | ({"file_id", "object_key", "bytes", "page_revision"} if internal else set())
            _require(set(page) <= allowed)
            page_id, number = _uuid(page["page_id"]), _positive(page["number"])
            _require(number <= 200 and page_id not in seen_ids and number not in seen_numbers)
            seen_ids.add(page_id)
            seen_numbers.add(number)
            region = page.get("region")
            _require((mode == "region") == (region is not None))
            if region is not None:
                _region(region)
            if "page_revision" in page:
                _positive(page["page_revision"])
            if internal:
                frozen_file_id = _uuid(page["file_id"])
                _require(file(frozen_file_id, owner)["mime"] != "application/pdf")
                _positive(page["bytes"])
                keep(page["object_key"])
                _require(key_owners.get(page["object_key"], {owner}) == {owner})
                # Page replacement/reordering is an access concern. A historical
                # same-owner page identity still proves this is a retained ref.
                _require((owner, note_id, frozen_file_id) in historical_pages.get(page_id, set()))
            else:
                actual = pages[page_id]
                _require(_uuid(actual["owner_id"]) == owner and _uuid(actual["note_id"]) == note_id)
                file(actual["file_id"], owner)
                _require(actual["pdf_page_number"] is None if kind == "image"
                         else actual["pdf_page_number"] == number)

    runs = {_uuid(row["id"]): row for row in rows(
        "ai_runs", "id,owner_id,conversation_id,status,lease_until,source")}
    active_ai = 0
    for row in runs.values():
        owner = _uuid(row["owner_id"])
        _require(conversations[_uuid(row["conversation_id"])] == owner)
        _require(row["status"] in {"queued", "running", "succeeded", "cancelled", "interrupted", "failed", "truncated"})
        active_ai += row["status"] in {"queued", "running"} or row["lease_until"] is not None
        source(row["source"], owner, internal=True)

    for row in rows("ai_saved_notes", "id,owner_id,run_id,note_id,source,cited_page_numbers"):
        owner = _uuid(row["owner_id"])
        note(row["note_id"], owner, "text")
        _require(_uuid(runs[_uuid(row["run_id"])]["owner_id"]) == owner)
        source(row["source"], owner, internal=False)
        _require(isinstance(row["cited_page_numbers"], list))
        if row["source"] is None or row["source"].get("kind") == "text":
            _require(not row["cited_page_numbers"])
        else:
            allowed = {page["number"] for page in row["source"]["pages"]}
            _require(all(type(number) is int and number in allowed for number in row["cited_page_numbers"]))

    active_jobs = 0
    for row in rows("jobs", "id,owner_id,kind,status,lease_until,worker_token IS NOT NULL AS has_worker_token,"
                    "CASE WHEN kind IN ('image.process','pdf.import') THEN payload ELSE NULL END AS payload,"
                    "result->'file_id' AS result_file_id,result->'import_id' AS result_import_id"):
        owner = _uuid(row["owner_id"])
        _require(row["kind"] in {"image.process", "pdf.import", "diagnostic"}
                 and row["status"] in {"queued", "running", "succeeded", "failed"})
        active_jobs += row["status"] in {"queued", "running"} or row["lease_until"] is not None or row["has_worker_token"]
        if row["kind"] == "image.process":
            _require(isinstance(row["payload"], dict) and set(row["payload"]) == {"file_id"})
            target = _uuid(row["payload"]["file_id"])
            file(target, owner)
            _require(row["result_file_id"] is None or _uuid(row["result_file_id"]) == target)
            _require(row["result_import_id"] is None)
        elif row["kind"] == "pdf.import":
            _require(isinstance(row["payload"], dict) and set(row["payload"]) == {"import_id"})
            target = _uuid(row["payload"]["import_id"])
            _require(_uuid(imports[target]["owner_id"]) == owner)
            _require(row["result_import_id"] is None or _uuid(row["result_import_id"]) == target)
            _require(row["result_file_id"] is None)
        else:
            _require(row["result_file_id"] is None and row["result_import_id"] is None)

    # A ticket cannot make deleted content accessible, but its relationship is
    # still validated and fingerprinted; no token hash is ever read.
    for row in rows("file_tickets", "id,owner_id,file_id,expires_at"):
        file(row["file_id"], _uuid(row["owner_id"]))

    counts.update(keys=len(keys), active_uploads=int(active_uploads), active_pdf_imports=int(active_imports),
                  active_ai_runs=int(active_ai), active_jobs=int(active_jobs),
                  text_source_versions=len(text_versions))
    snapshot["text_source_versions"] = {key: {"title": value[0], "text_sha256": hashlib.sha256(value[1].encode("utf-8")).hexdigest()}
                                        for key, value in text_versions.items()}
    return {"keys": keys, "counts": counts,
            "active_writers": int(active_uploads + active_imports + active_ai + active_jobs),
            "fingerprint": hashlib.sha256(_encoded({"snapshot": snapshot, "keys": sorted(keys)})).hexdigest()}
