"""Build a bounded, private account archive from a consistent database snapshot."""

import hashlib
import json
import tempfile
import time
import uuid
import zipfile
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.modules.ai.models import AiConversation, AiRun, AiSavedNote, AiStep
from app.modules.ai.source_context import public_source
from app.modules.files import service as files
from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes.models import Folder, Note, Notebook, NoteTag, NoteVersion, Tag
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.errors import AppError
from app.shared.models import AuthSession, Job, User


# Deliberately exclude credentials, sessions, request hashes, leases and provider inputs.
FIELDS = {
    Folder: ("id", "created_at", "owner_id", "name", "parent_id", "revision", "updated_at"),
    Notebook: ("id", "created_at", "owner_id", "name", "folder_id", "revision", "updated_at"),
    Tag: ("id", "created_at", "owner_id", "name", "revision", "updated_at"),
    Note: ("id", "created_at", "owner_id", "title", "kind", "notebook_id", "body_json", "plain_text", "deleted_at", "revision", "updated_at"),
    NoteTag: ("owner_id", "note_id", "tag_id"),
    NoteVersion: ("id", "created_at", "owner_id", "note_id", "revision", "snapshot"),
    File: ("id", "created_at", "owner_id", "filename", "mime", "expected_bytes", "bytes", "sha256", "width", "height", "status", "error_code"),
    NotePage: ("id", "created_at", "owner_id", "note_id", "file_id", "position", "revision", "pdf_page_number"),
    FileVariant: ("id", "created_at", "file_id", "kind", "bytes"),
    PdfImport: ("id", "created_at", "owner_id", "note_id", "source_file_id", "status", "page_count", "completed_pages", "error_code"),
    AiConversation: ("id", "created_at", "owner_id", "title"),
    AiRun: ("id", "created_at", "owner_id", "conversation_id", "model", "prompt", "answer", "status", "usage", "error_code", "finished_at", "source", "progress"),
    AiStep: ("id", "created_at", "run_id", "position", "kind", "pages", "status", "answer", "usage", "error_code"),
    AiSavedNote: ("id", "created_at", "owner_id", "run_id", "note_id", "source", "cited_page_numbers"),
}


def check_session(owner_id, auth_session_id):
    """Read current auth state outside the export's repeatable-read transaction."""
    try:
        with SessionLocal() as db:
            session = db.get(AuthSession, auth_session_id)
            user = db.get(User, owner_id)
            if (not session or session.user_id != owner_id or session.revoked_at
                    or session.expires_at <= utcnow() or not user or user.status != "active"):
                raise AppError(401, "login_required", "会话已失效，请重新登录")
    except SQLAlchemyError:
        raise AppError(503, "export_unavailable", "导出服务暂不可用，请稍后重试") from None


def _default(value):
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    raise TypeError("unsupported_export_value")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_default).encode("utf-8")


def _incomplete():
    return AppError(409, "export_incomplete", "文件或来源信息不完整，无法生成完整导出，请联系管理员检查")


def _too_large():
    return AppError(413, "export_too_large", "数据超过本次导出上限，请联系管理员调整导出限制")


def create_archive(owner_id, auth_session_id):
    """Return an open TemporaryFile; caller owns closing it after transmission."""
    cfg = settings()
    deadline = time.monotonic() + cfg.export_max_seconds
    snapshot_open = False

    def checkpoint():
        if time.monotonic() >= deadline:
            raise AppError(503, "export_timeout", "导出耗时超过上限，请稍后重试")
        # A one-connection pool must not wait on itself while taking the snapshot.
        # Current auth is checked again immediately after releasing the snapshot.
        if not snapshot_open:
            check_session(owner_id, auth_session_id)

    checkpoint()
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        with SessionLocal() as db:
            snapshot_open = True
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            # The export cannot accidentally keep a DB read running beyond its budget.
            db.execute(text(f"SET LOCAL statement_timeout = {max(1, int(cfg.export_max_seconds * 1000))}"))
            owner = db.get(User, owner_id)
            if not owner or owner.status != "active":
                raise AppError(401, "login_required", "会话已失效，请重新登录")
            owner_profile = {"id": str(owner.id), "username": owner.username, "display_name": owner.display_name}
            busy_job = db.scalar(select(Job.id).where(Job.owner_id == owner_id,
                Job.kind.in_(("image.process", "pdf.import")), Job.status.in_(("queued", "running"))).limit(1))
            busy_ai = db.scalar(select(AiRun.id).where(AiRun.owner_id == owner_id,
                AiRun.status.in_(("queued", "running"))).limit(1))
            busy_upload = db.scalar(select(File.id).where(File.owner_id == owner_id,
                File.status == "uploading").limit(1))
            if busy_job or busy_ai or busy_upload:
                raise AppError(409, "export_busy", "请等待上传、图片/PDF 处理和 AI 回复完成；过期上传请等待回收后重试")

            collections, internal_keys = {}, {}
            total_rows, metadata_size = 0, 0
            for model, names in FIELDS.items():
                checkpoint()
                columns = [getattr(model, name) for name in names]
                if model in (File, FileVariant):
                    columns.append(model.object_key)
                query = select(*columns)
                if model is FileVariant:
                    query = query.join(File, File.id == FileVariant.file_id).where(File.owner_id == owner_id)
                elif model is AiStep:
                    query = query.join(AiRun, AiRun.id == AiStep.run_id).where(AiRun.owner_id == owner_id)
                else:
                    query = query.where(model.owner_id == owner_id)
                order = (model.note_id, model.tag_id) if model is NoteTag else (model.id,)
                query = query.order_by(*order).limit(cfg.export_max_rows - total_rows + 1).execution_options(yield_per=1)
                db.execute(text(f"SET LOCAL statement_timeout = {max(1, int((deadline - time.monotonic()) * 1000))}"))
                records = []
                for row in db.execute(query).mappings():
                    total_rows += 1
                    if total_rows > cfg.export_max_rows:
                        raise _too_large()
                    record = {name: row[name] for name in names}
                    if model in (File, FileVariant):
                        internal_keys[(model, record["id"])] = row["object_key"]
                    # Avoid constructing arbitrarily large metadata before checking it.
                    metadata_size += len(_json(record)) + 1
                    if metadata_size > cfg.export_max_metadata_bytes:
                        raise _too_large()
                    records.append(record)
                    if len(records) % 100 == 0:
                        checkpoint()
                collections[model.__tablename__] = records

            owned_files = {str(f["id"]): f for f in collections["files"]}
            owned_pages = {str(p["id"]): p for p in collections["note_pages"]}
            owned_notes = {str(n["id"]) for n in collections["notes"]}
            reading = {}
            variant_kinds = {}
            objects = []
            for file in collections["files"]:
                file_id = file["id"]
                file["original_path"] = None
                if file["status"] == "expired":
                    file["original_unavailable_reason"] = "upload_expired"
                    continue
                if file["status"] == "rejected" and file["mime"] != "application/pdf":
                    # InvalidImage processing deliberately removes rejected input.
                    file["original_unavailable_reason"] = "image_rejected"
                    continue
                if not file["bytes"]:
                    raise _incomplete()
                file["original_path"] = f"objects/{file_id}/original"
                objects.append((file["original_path"], internal_keys[(File, file_id)], file["bytes"], file["sha256"]))
            for variant in collections["file_variants"]:
                variant["path"] = f"objects/{variant['file_id']}/variants/{variant['id']}"
                objects.append((variant["path"], internal_keys[(FileVariant, variant["id"])], variant["bytes"], None))
                if variant["kind"] == "reading":
                    reading[str(variant["file_id"])] = variant
                variant_kinds.setdefault(str(variant["file_id"]), set()).add(variant["kind"])
            for file_id, file in owned_files.items():
                if (file["status"] == "ready" and file["mime"] != "application/pdf"
                        and variant_kinds.get(file_id) != {"reading", "thumbnail"}):
                    raise _incomplete()
            pdf_pages = {}
            for page in owned_pages.values():
                if page["pdf_page_number"] is not None:
                    pdf_pages.setdefault(page["note_id"], []).append(page)
            for item in collections["pdf_imports"]:
                if item["status"] == "ready":
                    pages = pdf_pages.get(item["note_id"], [])
                    if (not item["page_count"] or len(pages) != item["page_count"] or item["completed_pages"] != item["page_count"]
                            or sorted(p["pdf_page_number"] for p in pages) != list(range(1, item["page_count"] + 1))
                            or any(owned_files[str(p["file_id"])]["status"] != "ready" for p in pages)):
                        raise _incomplete()

            # Preserve terminal processing failures without exporting job payloads or leases.
            failed_file_ids = [f["id"] for f in collections["files"] if f["status"] == "processing"]
            if failed_file_ids:
                failed_jobs = db.execute(select(Job.dedupe_key, Job.status, Job.error_code).where(
                    Job.owner_id == owner_id, Job.kind == "image.process", Job.status == "failed",
                    Job.dedupe_key.in_([f"image_{file_id}" for file_id in failed_file_ids])))
                failed = {row.dedupe_key: row for row in failed_jobs}
                for file_id in failed_file_ids:
                    if job := failed.get(f"image_{file_id}"):
                        owned_files[str(file_id)]["processing_status"] = job.status
                        owned_files[str(file_id)]["processing_error_code"] = job.error_code

            for table in ("ai_runs", "ai_saved_notes"):
                for index, record in enumerate(collections[table]):
                    if index % 100 == 0:
                        checkpoint()
                    source = record["source"]
                    if source is None:
                        continue
                    try:
                        safe = public_source(source).model_dump(mode="json")
                        if safe["note_id"] not in owned_notes or not safe["pages"]:
                            raise ValueError("foreign_source_note")
                        if safe["kind"] == "image" and (len(safe["pages"]) != 1
                                or (safe["mode"] == "region") != (safe["pages"][0].get("region") is not None)):
                            raise ValueError("invalid_image_source")
                        for frozen, page in zip(source["pages"], safe["pages"], strict=True):
                            actual = owned_pages.get(page["page_id"])
                            if not actual or str(actual["note_id"]) != safe["note_id"]:
                                raise ValueError("source_page_missing")
                            file_id = str(actual["file_id"])
                            variant = reading.get(file_id)
                            if file_id not in owned_files or not variant:
                                raise ValueError("source_variant_missing")
                            if (frozen.get("file_id", file_id) != file_id
                                    or frozen.get("object_key", internal_keys[(FileVariant, variant["id"])]) != internal_keys[(FileVariant, variant["id"])]
                                    or frozen.get("bytes", variant["bytes"]) != variant["bytes"]
                                    or safe["kind"] == "pdf" and page["number"] != actual["pdf_page_number"]
                                    or safe["kind"] == "image" and actual["pdf_page_number"] is not None):
                                raise ValueError("source_identity_changed")
                            page["file_id"], page["reading_path"] = file_id, variant["path"]
                        record["source"] = safe
                    except (TypeError, ValueError, KeyError):
                        raise _incomplete() from None

            checkpoint()
            data = _json({"collections": collections})
            if len(data) > cfg.export_max_metadata_bytes:
                raise _too_large()
            data_bytes = len(data)
            expected_total = data_bytes + sum(item[2] for item in objects)
            if expected_total > cfg.export_max_bytes:
                raise _too_large()
            payloads = [{"path": "data.json", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
            # All metadata is now detached plain data. Avoid keeping a database
            # transaction or connection open while reading/compressing objects.
            db.rollback()
            db.close()
            snapshot_open = False
            checkpoint()
            try:
                storage = files.storage()
            except Exception:
                raise AppError(503, "storage_unavailable", "文件存储暂不可用，请稍后重试") from None
            with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("data.json", data)
                del data
                for path, key, expected_bytes, expected_sha in objects:
                    checkpoint()
                    try:
                        info = storage.head(key)
                        if info.size != expected_bytes:
                            raise _incomplete()
                        if info.size > max(cfg.upload_max_bytes, cfg.pdf_max_bytes):
                            raise _too_large()
                        content = storage.read(key)
                    except FileNotFoundError:
                        raise _incomplete() from None
                    except AppError:
                        raise
                    except Exception:
                        # Storage providers may put signed URLs or secrets into exceptions.
                        raise AppError(503, "storage_unavailable", "文件存储暂不可用，请稍后重试") from None
                    checkpoint()
                    checksum = hashlib.sha256(content).hexdigest()
                    if len(content) != expected_bytes or expected_sha and checksum != expected_sha:
                        raise _incomplete()
                    archive.writestr(path, content)
                    payloads.append({"path": path, "bytes": len(content), "sha256": checksum})
                    del content
                manifest = {
                    "format": "ai-notes-export", "version": 1, "exported_at": utcnow().isoformat(),
                    "owner": owner_profile,
                    "counts": {name: len(records) for name, records in collections.items()},
                    "resume_processing": False, "payloads": payloads,
                }
                manifest_bytes = _json(manifest)
                if (data_bytes + len(manifest_bytes) > cfg.export_max_metadata_bytes
                        or expected_total + len(manifest_bytes) > cfg.export_max_bytes):
                    raise _too_large()
                archive.writestr("manifest.json", manifest_bytes)
            checkpoint()
        output.seek(0)
        return output
    except SQLAlchemyError:
        output.close()
        raise AppError(503, "export_unavailable", "导出服务暂不可用，请稍后重试") from None
    except BaseException:
        output.close()
        raise
