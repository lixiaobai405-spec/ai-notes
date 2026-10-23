"""Verify an account export without extracting it or contacting any service.

Historical snapshots may refer to organizations that have since been removed.
Those historical references are preserved, not validated as current foreign keys;
a future importer must remap them according to its documented restore policy.
Checksums detect damage, not malicious replacement of both data and its manifest.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from uuid import UUID
import zipfile


FORMAT = "ai-notes-export"
VERSION = 1
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_ROWS = 50_000
MAX_ENTRIES = 50_002
CHUNK_BYTES = 256 * 1024
TABLES = (
    "folders", "notebooks", "tags", "notes", "note_tags", "note_versions",
    "files", "note_pages", "file_variants", "pdf_imports", "ai_conversations",
    "ai_runs", "ai_steps", "ai_saved_notes",
)
OWNED_TABLES = set(TABLES) - {"file_variants", "ai_steps"}
UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ASSET_PATH = re.compile(rf"objects/({UUID_TEXT})/(?:original|variants/{UUID_TEXT})")


class ValidationError(ValueError):
    """A short, non-sensitive validation code suitable for CLI output."""


def _require(condition, code):
    if not condition:
        raise ValidationError(code)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _uuid(value):
    if not isinstance(value, str):
        raise ValidationError("invalid_identifier")
    try:
        _require(str(UUID(value)) == value, "invalid_identifier")
    except ValueError:
        raise ValidationError("invalid_identifier") from None
    return value


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValidationError("invalid_json")

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)


def _metadata(archive, entry):
    _require(entry.file_size <= MAX_METADATA_BYTES, "metadata_too_large")
    with archive.open(entry) as stream:
        data = stream.read(MAX_METADATA_BYTES + 1)
    _require(len(data) <= MAX_METADATA_BYTES, "metadata_too_large")
    return _json(data)


def _references(collections, payloads, owner):
    payload_paths = set(payloads)
    indexed = {}
    rows = 0
    for table in TABLES:
        items = collections[table]
        _require(isinstance(items, list), "invalid_collection")
        rows += len(items)
        _require(rows <= MAX_ROWS, "too_many_rows")
        by_id = {}
        for row in items:
            _require(isinstance(row, dict), "invalid_record")
            if table in OWNED_TABLES:
                _require(row.get("owner_id") == owner, "owner_mismatch")
            if table != "note_tags":
                identity = _uuid(row.get("id"))
                _require(identity not in by_id, "duplicate_record")
                by_id[identity] = row
        indexed[table] = by_id

    def ref(row, field, table, nullable=False):
        value = row.get(field)
        if nullable and value is None:
            return
        _require(_uuid(value) in indexed[table], "missing_reference")

    for row in collections["folders"]:
        ref(row, "parent_id", "folders", nullable=True)
        _require(row.get("parent_id") != row["id"], "invalid_folder_parent")
    # The live model forbids cycles; reject cycles even if every ID exists.
    completed = set()
    for identity in indexed["folders"]:
        visiting = set()
        cursor = identity
        while cursor is not None and cursor not in completed:
            _require(cursor not in visiting, "invalid_folder_parent")
            visiting.add(cursor)
            cursor = indexed["folders"][cursor].get("parent_id")
        completed.update(visiting)
    for row in collections["notebooks"]:
        ref(row, "folder_id", "folders", nullable=True)
    for row in collections["notes"]:
        ref(row, "notebook_id", "notebooks", nullable=True)
    note_tags = set()
    for row in collections["note_tags"]:
        ref(row, "note_id", "notes")
        ref(row, "tag_id", "tags")
        identity = (row["note_id"], row["tag_id"])
        _require(identity not in note_tags, "duplicate_record")
        note_tags.add(identity)
    for row in collections["note_versions"]:
        ref(row, "note_id", "notes")

    used_assets = set()
    for row in collections["files"]:
        path = row.get("original_path")
        if path is None:
            expired = row.get("status") == "expired" and row.get("original_unavailable_reason") == "upload_expired"
            rejected_image = (row.get("status") == "rejected" and row.get("mime") != "application/pdf" and
                              row.get("original_unavailable_reason") == "image_rejected")
            _require(expired or rejected_image, "missing_asset")
        else:
            _require(path == f"objects/{row['id']}/original" and path in payload_paths,
                     "missing_asset")
            _require(_integer(row.get("bytes")) and row["bytes"] > 0 and
                     row["bytes"] == payloads[path]["bytes"] and
                     (row.get("sha256") is None or row["sha256"] == payloads[path]["sha256"]),
                     "asset_metadata_mismatch")
            used_assets.add(path)
    variants = {}
    for row in collections["file_variants"]:
        ref(row, "file_id", "files")
        path = row.get("path")
        _require(path == f"objects/{row['file_id']}/variants/{row['id']}" and
                 path in payload_paths, "missing_asset")
        _require(row.get("kind") in {"reading", "thumbnail"} and
                 _integer(row.get("bytes")) and row["bytes"] > 0 and
                 row["bytes"] == payloads[path]["bytes"], "asset_metadata_mismatch")
        identity = (row["file_id"], row.get("kind"))
        _require(identity not in variants, "duplicate_record")
        variants[identity] = row
        used_assets.add(path)
    positions = set()
    for row in collections["note_pages"]:
        ref(row, "note_id", "notes")
        ref(row, "file_id", "files")
        position = row.get("position")
        _require(_integer(position) and position >= 0, "invalid_page")
        identity = (row["note_id"], position)
        _require(identity not in positions, "duplicate_record")
        positions.add(identity)
    for row in collections["pdf_imports"]:
        ref(row, "note_id", "notes")
        ref(row, "source_file_id", "files")
    for row in collections["ai_runs"]:
        ref(row, "conversation_id", "ai_conversations")
    for row in collections["ai_steps"]:
        ref(row, "run_id", "ai_runs")
    for row in collections["ai_saved_notes"]:
        ref(row, "note_id", "notes")
        ref(row, "run_id", "ai_runs")

    for row in collections["ai_runs"] + collections["ai_saved_notes"]:
        source = row.get("source")
        if source is None:
            continue
        _require(isinstance(source, dict), "invalid_source")
        ref(source, "note_id", "notes")
        pages = source.get("pages")
        _require(isinstance(pages, list) and 0 < len(pages) <= MAX_ROWS, "invalid_source")
        for page in pages:
            _require(isinstance(page, dict), "invalid_source")
            ref(page, "page_id", "note_pages")
            ref(page, "file_id", "files")
            actual = indexed["note_pages"][page["page_id"]]
            _require(actual["note_id"] == source["note_id"] and
                     actual["file_id"] == page["file_id"], "source_mismatch")
            reading = variants.get((page["file_id"], "reading"))
            _require(reading is not None and page.get("reading_path") == reading["path"],
                     "source_mismatch")
    _require(used_assets == payload_paths - {"data.json"}, "unreferenced_asset")


def verify_archive(path):
    """Return table counts after integrity checks; never extract archive entries."""
    try:
        _require(Path(path).stat().st_size <= MAX_TOTAL_BYTES + 16 * 1024 * 1024,
                 "archive_too_large")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            _require(2 <= len(entries) <= MAX_ENTRIES, "invalid_entry_count")
            by_path = {}
            for entry in entries:
                name = entry.filename
                _require(entry.orig_filename == name, "unsafe_entry_path")
                _require(name in {"manifest.json", "data.json"} or
                         ASSET_PATH.fullmatch(name) is not None, "unsafe_entry_path")
                _require(name not in by_path, "duplicate_entry")
                mode = entry.external_attr >> 16
                _require(not entry.is_dir() and not stat.S_ISLNK(mode) and
                         stat.S_IFMT(mode) in {0, stat.S_IFREG}, "invalid_entry_type")
                _require(not entry.flag_bits & 1 and entry.compress_type in
                         {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}, "unsupported_entry")
                by_path[name] = entry
            _require({"manifest.json", "data.json"} <= by_path.keys(), "missing_metadata")
            _require(sum(by_path[name].file_size for name in ("manifest.json", "data.json"))
                     <= MAX_METADATA_BYTES, "metadata_too_large")
            manifest = _metadata(archive, by_path["manifest.json"])
            _require(isinstance(manifest, dict) and manifest.get("format") == FORMAT and
                     type(manifest.get("version")) is int and manifest["version"] == VERSION,
                     "unsupported_format")
            _require(manifest.get("resume_processing") is False, "unsafe_processing_state")
            timestamp = manifest.get("exported_at")
            _require(isinstance(timestamp, str) and
                     datetime.fromisoformat(timestamp).utcoffset() is not None, "invalid_timestamp")
            owner = manifest.get("owner")
            _require(isinstance(owner, dict), "invalid_owner")
            owner_id = _uuid(owner.get("id"))
            _require(all(isinstance(owner.get(field), str) for field in ("username", "display_name")),
                     "invalid_owner")
            payloads = manifest.get("payloads")
            _require(isinstance(payloads, list) and 1 <= len(payloads) < MAX_ENTRIES,
                     "invalid_payloads")
            declared = {}
            total = by_path["manifest.json"].file_size
            for item in payloads:
                _require(isinstance(item, dict), "invalid_payload")
                name, size, checksum = item.get("path"), item.get("bytes"), item.get("sha256")
                _require(isinstance(name, str) and name != "manifest.json" and
                         name in by_path and name not in declared, "invalid_payload")
                _require(_integer(size) and size >= 0 and
                         isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum),
                         "invalid_payload")
                total += size
                _require(total <= MAX_TOTAL_BYTES, "payloads_too_large")
                _require(by_path[name].file_size == size, "size_mismatch")
                declared[name] = item
            _require(set(declared) == set(by_path) - {"manifest.json"}, "payload_set_mismatch")
            for name, item in declared.items():
                digest, actual_size = hashlib.sha256(), 0
                with archive.open(by_path[name]) as stream:
                    while chunk := stream.read(CHUNK_BYTES):
                        actual_size += len(chunk)
                        _require(actual_size <= item["bytes"], "size_mismatch")
                        digest.update(chunk)
                _require(actual_size == item["bytes"] and digest.hexdigest() == item["sha256"],
                         "checksum_mismatch")
            data = _metadata(archive, by_path["data.json"])
            _require(isinstance(data, dict) and set(data) == {"collections"} and
                     isinstance(data["collections"], dict) and
                     set(data["collections"]) == set(TABLES), "invalid_collections")
            collections = data["collections"]
            counts = manifest.get("counts")
            _require(isinstance(counts, dict) and set(counts) == set(TABLES), "invalid_counts")
            for table in TABLES:
                _require(isinstance(collections[table], list) and _integer(counts[table]) and
                         counts[table] == len(collections[table]), "count_mismatch")
            _references(collections, declared, owner_id)
            return dict(counts)
    except ValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RuntimeError,
            zipfile.BadZipFile, EOFError, RecursionError, NotImplementedError):
        raise ValidationError("archive_invalid") from None


def main():
    parser = argparse.ArgumentParser(description="离线校验知页账号导出包，不解压、不恢复。")
    parser.add_argument("archive", help="导出 ZIP 文件")
    arguments = parser.parse_args()
    try:
        counts = verify_archive(arguments.archive)
    except ValidationError as error:
        print(f"校验失败：{error}", file=sys.stderr)
        return 1
    print(f"校验成功：{len(counts)} 类数据，共 {sum(counts.values())} 条记录。未执行恢复。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
