"""Manual, fail-closed private file cleanup. Default action is a read-only plan.

Run from the repository root with the backend virtual environment:
    backend/.venv/Scripts/python.exe -m scripts.file_gc --config private.json plan
No credentials are accepted as command-line values or printed in diagnostics.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

from scripts.file_gc_common import (FileGCError, ObjectRecord, MAX_OBJECTS,
    MAX_OBJECT_BYTES, MAX_TOTAL_BYTES, MIN_GRACE_DAYS, require, timestamp)
from scripts.file_gc_state import PrivateState, validate_bundle

FORMAT = "ai-notes-file-gc-plan"
BUNDLE_FORMAT = "ai-notes-file-gc-bundle"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NATIVE = re.compile(rf"{UUID}/{UUID}/(?:original|{UUID}-(?:original|reading|thumbnail)\.jpg)\Z")
RESTORED = re.compile(r"restore_[0-9a-f]{32}/[0-9a-f]{32}/(?:original|v_[0-9a-f]{32})\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
PLAN_FIELDS = {"format", "version", "id", "created_at", "grace_days", "backend",
               "reference_fingerprint", "summary", "objects", "digest"}
LOCK_TABLES = ("users", "notes", "note_versions", "note_pages", "files", "file_variants",
               "upload_sessions", "pdf_imports", "jobs", "ai_conversations", "ai_runs", "ai_saved_notes", "file_tickets")


def digest(document):
    try:
        return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise FileGCError("gc_document_invalid") from None


def known_key(key):
    return isinstance(key, str) and len(key) <= 256 and bool(NATIVE.fullmatch(key) or RESTORED.fullmatch(key))


def record(document):
    require(isinstance(document, dict) and set(document) == {"key", "size", "modified_at", "identity"}, "gc_plan_invalid")
    require(known_key(document["key"]) and type(document["size"]) is int
            and 0 <= document["size"] <= MAX_OBJECT_BYTES
            and isinstance(document["identity"], dict) and bool(document["identity"]), "gc_plan_invalid")
    timestamp(document["modified_at"])
    return ObjectRecord(**document)


def bundle_digest(manifest):
    # Restore progress is mutable. Original material and deletion receipts remain
    # bound to the operator-reviewed digest across retries and partial recovery.
    return digest({key: ([{k: v for k, v in item.items() if k not in {"restore_status", "restore_receipt"}}
                         for item in value] if key == "objects" else value)
                   for key, value in manifest.items() if key not in {"phase", "digest", "error_code"}})


class DatabaseReferences:
    """Read only application projections; guard mutations with global exclusion.

    Table SHARE locks prevent publication while the collector acts. They do not
    stop a PUT already in flight: the required operator attestation covers all
    API/Worker/CLI/external writers, and the job/upload checks are an extra gate.
    """
    def __init__(self, url):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        require(parsed.get_backend_name() == "postgresql" and parsed.database, "gc_database_invalid")
        self.engine = create_engine(url, hide_parameters=True, pool_pre_ping=True)

    @contextmanager
    def snapshot(self, writing=False):
        from sqlalchemy import text
        backend = str(Path(__file__).resolve().parents[1] / "backend")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from app.modules.files.retention import collect_references
        try:
            with self.engine.connect() as connection, connection.begin():
                connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                connection.execute(text("SET LOCAL lock_timeout = '2s'"))
                if writing:
                    require(connection.scalar(text("SELECT pg_try_advisory_xact_lock(7135381, 412)")) is True, "gc_locked")
                    connection.execute(text("LOCK TABLE " + ",".join("public." + table for table in LOCK_TABLES) + " IN SHARE MODE NOWAIT"))
                else:
                    connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                yield collect_references(connection)
        except FileGCError:
            raise
        except Exception:
            raise FileGCError("gc_references_unavailable") from None


class FileCollector:
    def __init__(self, storage, state, references, *, now=None, timeout=600):
        self.storage, self.state, self.references = storage, state, references
        self.now = now or (lambda: datetime.now(timezone.utc))
        require(type(timeout) is int and 1 <= timeout <= 3600, "gc_limit_invalid")
        self.timeout = timeout
        identity = storage.backend_identity()
        require(isinstance(identity, dict) and identity.get("backend") in {"local", "oss"}, "gc_backend_invalid")
        if identity["backend"] == "local":
            root, state_root = Path(identity["root"]).resolve(), state.root.resolve()
            require(not root.is_relative_to(state_root) and not state_root.is_relative_to(root), "gc_paths_overlap")

    def checkpoint(self, started):
        require(time.monotonic() - started < self.timeout, "gc_timeout")

    def start(self):
        started = time.monotonic()
        self.storage.checkpoint = lambda: self.checkpoint(started)
        return started

    def retained(self, graph):
        # Local object keys can have multiple spellings on Windows. Conservatively
        # retain equivalent ASCII case/trailing-dot-space spellings on every local
        # filesystem; over-retention on a case-sensitive host is safe. OSS keys
        # remain exactly case-sensitive and are never normalized.
        if self.storage.backend_identity()["backend"] == "local":
            return {"/".join(part.rstrip(" .").casefold() for part in key.split("/")) for key in graph["keys"]}
        return graph["keys"]

    def plan(self, *, grace_days=MIN_GRACE_DAYS):
        require(type(grace_days) is int and MIN_GRACE_DAYS <= grace_days <= 3650, "gc_grace_invalid")
        started = self.start()
        now = self.now(); cutoff = now - timedelta(days=grace_days)
        with self.state.lock(), self.references.snapshot() as graph:
            retained = self.retained(graph)
            inventory = self.storage.inventory(limit=MAX_OBJECTS)
            self.checkpoint(started)
            require(len(inventory) <= MAX_OBJECTS and len({item.key for item in inventory}) == len(inventory), "gc_inventory_invalid")
            summary = dict.fromkeys(("total", "referenced", "unknown", "young", "oversized", "candidates", "candidate_bytes"), 0)
            summary["total"] = len(inventory)
            candidates = []
            for item in sorted(inventory, key=lambda item: item.key):
                self.checkpoint(started)
                if item.key in retained:
                    summary["referenced"] += 1
                elif not known_key(item.key):
                    summary["unknown"] += 1
                elif timestamp(item.modified_at) >= cutoff:
                    summary["young"] += 1
                elif type(item.size) is not int or not 0 <= item.size <= MAX_OBJECT_BYTES:
                    summary["oversized"] += 1
                else:
                    candidates.append(record(item.document()).document())
                    summary["candidate_bytes"] += item.size
            require(summary["candidate_bytes"] <= MAX_TOTAL_BYTES, "gc_total_size_exceeded")
            summary["candidates"] = len(candidates)
            summary["active_writers"] = graph["active_writers"]
            document = {"format": FORMAT, "version": 1, "id": uuid4().hex,
                "created_at": now.isoformat(), "grace_days": grace_days,
                "backend": self.storage.backend_identity(), "reference_fingerprint": graph["fingerprint"],
                "summary": summary, "objects": candidates}
            document["digest"] = digest(document)
            name = "plan-" + document["id"] + ".json"
            self.state.write(name, document)
            return {"status": "planned", "plan": name, "digest": document["digest"], **summary}

    def read_plan(self, name, expected):
        value = self.state.read(name)
        require(set(value) == PLAN_FIELDS and value["format"] == FORMAT and type(value["version"]) is int
                and value["version"] == 1 and isinstance(expected, str) and HEX.fullmatch(expected), "gc_plan_invalid")
        require(value["digest"] == expected == digest({k: v for k, v in value.items() if k != "digest"}), "gc_plan_changed")
        require(value["backend"] == self.storage.backend_identity(), "gc_backend_changed")
        require(type(value["grace_days"]) is int and MIN_GRACE_DAYS <= value["grace_days"] <= 3650, "gc_plan_invalid")
        age = self.now() - timestamp(value["created_at"])
        require(timedelta(0) <= age <= timedelta(hours=24), "gc_plan_expired")
        objects = value["objects"]
        require(isinstance(objects, list) and len(objects) <= MAX_OBJECTS, "gc_plan_invalid")
        candidates = [record(item) for item in objects]
        require(len({item.key for item in candidates}) == len(candidates)
                and sum(item.size for item in candidates) <= MAX_TOTAL_BYTES, "gc_plan_invalid")
        cutoff = timestamp(value["created_at"]) - timedelta(days=value["grace_days"])
        require(all(timestamp(item.modified_at) < cutoff for item in candidates), "gc_plan_invalid")
        require(isinstance(value["reference_fingerprint"], str) and HEX.fullmatch(value["reference_fingerprint"]), "gc_plan_invalid")
        return value, candidates

    def attest(self, maintenance_confirmed, writers_stopped):
        require(maintenance_confirmed is True and writers_stopped is True, "gc_maintenance_required")

    def apply(self, name, expected, *, maintenance_confirmed=False, writers_stopped=False):
        self.attest(maintenance_confirmed, writers_stopped)
        started = self.start()
        with self.state.lock():
            plan, candidates = self.read_plan(name, expected)
            self.storage.ensure_writable()
            with self.references.snapshot(writing=True) as graph:
                require(graph["active_writers"] == 0, "gc_writers_active")
                require(graph["fingerprint"] == plan["reference_fingerprint"], "gc_references_changed")
                retained = self.retained(graph)
                # Revalidate the entire operation before archiving or deleting
                # any candidate. The database guard holds through completion.
                for item in candidates:
                    self.checkpoint(started)
                    require(item.key not in retained, "gc_references_changed")
                    require(self.storage.head(item.key).document() == item.document(), "gc_object_changed")
                if not candidates:
                    return {"status": "nothing_to_delete", "deleted": 0}
                bundle_id = "run-" + uuid4().hex
                bundle = self.state.create_bundle(bundle_id)
                journal = PrivateState(bundle)
                manifest = {"format": BUNDLE_FORMAT, "version": 1, "id": bundle_id,
                    "created_at": self.now().isoformat(), "backend": plan["backend"],
                    "plan_digest": expected, "phase": "archiving", "objects": []}
                journal.write("manifest.json", manifest)
                try:
                    for index, item in enumerate(candidates, 1):
                        self.checkpoint(started)
                        payload = f"objects/{index:06d}.bin"
                        captured = self.storage.export(item, bundle / payload)
                        require(captured.get("size") == item.size and isinstance(captured.get("sha256"), str)
                                and HEX.fullmatch(captured["sha256"]), "gc_archive_invalid")
                        manifest["objects"].append({"record": item.document(), "payload": payload,
                            "size": captured["size"], "sha256": captured["sha256"], "status": "archived", "receipt": None})
                        journal.write("manifest.json", manifest)
                    validate_bundle(bundle, manifest)
                    manifest["phase"] = "deleting"
                    journal.write("manifest.json", manifest)
                    for item in manifest["objects"]:
                        self.checkpoint(started)
                        original = record(item["record"])
                        require(self.storage.head(original.key).document() == original.document(), "gc_object_changed")
                        item["status"] = "deleting"
                        journal.write("manifest.json", manifest)  # durable before external delete
                        try:
                            receipt = self.storage.delete(original)
                            require(isinstance(receipt, dict) and receipt.get("status") == "deleted"
                                    and receipt.get("key") == original.key, "gc_delete_unknown")
                        except BaseException:
                            item["status"] = "unknown"
                            raise FileGCError("gc_delete_unknown") from None
                        item["status"], item["receipt"] = "deleted", receipt
                        journal.write("manifest.json", manifest)
                    manifest["phase"] = "complete"
                except BaseException as error:
                    manifest["phase"] = "stopped"
                    manifest["error_code"] = error.code if isinstance(error, FileGCError) else "gc_operation_failed"
                manifest["digest"] = bundle_digest(manifest)
                journal.write("manifest.json", manifest)
                result = self.summary(manifest)
                result["bundle"] = bundle_id
                return result

    def read_bundle(self, name):
        bundle = self.state.bundle(name)
        manifest = PrivateState(bundle).read("manifest.json")
        require(manifest.get("format") == BUNDLE_FORMAT and type(manifest.get("version")) is int
                and manifest["version"] == 1 and manifest.get("id") == name, "gc_bundle_invalid")
        require(manifest.get("backend") == self.storage.backend_identity(), "gc_backend_changed")
        require(isinstance(manifest.get("objects"), list) and len(manifest["objects"]) <= MAX_OBJECTS, "gc_bundle_invalid")
        seen = set()
        for item in manifest["objects"]:
            require(isinstance(item, dict), "gc_bundle_invalid")
            original = record(item.get("record"))
            require(original.key not in seen and original.size == item.get("size")
                    and item.get("status") in {"archived", "deleting", "deleted", "unknown"}, "gc_bundle_invalid")
            seen.add(original.key)
        validate_bundle(bundle, manifest)
        return bundle, manifest

    @staticmethod
    def summary(manifest):
        return {"status": manifest["phase"], "digest": bundle_digest(manifest),
            "archived": len(manifest["objects"]),
            "deleted": sum(item["status"] == "deleted" for item in manifest["objects"]),
            "unknown": sum(item["status"] in {"deleting", "unknown"} for item in manifest["objects"]),
            "restored": sum(item.get("restore_status") in {"restored", "already_present"} for item in manifest["objects"]),
            **({"error_code": manifest["error_code"]} if "error_code" in manifest else {})}

    def status(self, name):
        with self.state.lock():
            _, manifest = self.read_bundle(name)
            return {"bundle": name, **self.summary(manifest)}

    def restore(self, name, expected, *, maintenance_confirmed=False, writers_stopped=False):
        self.attest(maintenance_confirmed, writers_stopped)
        started = self.start()
        with self.state.lock():
            bundle, manifest = self.read_bundle(name)
            require(isinstance(expected, str) and HEX.fullmatch(expected)
                    and expected == bundle_digest(manifest), "gc_bundle_changed")
            self.storage.ensure_writable()
            with self.references.snapshot(writing=True) as graph:
                require(graph["active_writers"] == 0, "gc_writers_active")
                journal = PrivateState(bundle)
                manifest["phase"] = "restoring"
                manifest.pop("error_code", None)
                journal.write("manifest.json", manifest)
                try:
                    for item in manifest["objects"]:
                        self.checkpoint(started)
                        if item["status"] == "archived":
                            continue  # No delete was ever attempted.
                        original = record(item["record"])
                        receipt = self.storage.restore(original.key, bundle / item["payload"], size=item["size"],
                            sha256=item["sha256"], original=original, receipt=item.get("receipt"))
                        require(not isinstance(receipt, dict) or receipt.get("status") != "manual_required", "gc_restore_manual_required")
                        require(isinstance(receipt, dict) and receipt.get("status") in {"restored", "already_present"}, "gc_restore_failed")
                        item["restore_status"], item["restore_receipt"] = receipt["status"], receipt
                        journal.write("manifest.json", manifest)
                    manifest["phase"] = "restored"
                except BaseException as error:
                    manifest["phase"] = "restore_stopped"
                    manifest["error_code"] = error.code if isinstance(error, FileGCError) else "gc_restore_failed"
                journal.write("manifest.json", manifest)
                return {"bundle": name, **self.summary(manifest)}


def _variable(name, *, required=True):
    require(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name), "gc_config_invalid")
    value = os.environ.get(name, "")
    require(not required or bool(value), "gc_environment_missing")
    return value


def configured(path):
    # Configuration has paths and variable names only. Never accept literal
    # database URLs or credentials that might reach process arguments/logs.
    from scripts.file_gc_support import LocalGCStorage, OSSGCStorage
    config_path = Path(path).absolute()
    cfg = PrivateState(config_path.parent).read(config_path.name)
    require(set(cfg) == {"database_url_env", "state_root", "storage"}
            and isinstance(cfg["storage"], dict), "gc_config_invalid")
    database_url_env = cfg["database_url_env"]
    state = PrivateState(cfg["state_root"])
    store = cfg["storage"]
    if store.get("backend") == "local":
        require(set(store) == {"backend", "root"}, "gc_config_invalid")
        storage = LocalGCStorage(store["root"])
    else:
        require(set(store) == {"backend", "region", "bucket", "prefix", "access_key_id_env", "access_key_secret_env", "security_token_env"}
                and store["backend"] == "oss", "gc_config_invalid")
        from pydantic import SecretStr
        cfg = SimpleNamespace(oss_region=store["region"], oss_bucket=store["bucket"], oss_prefix=store["prefix"],
            oss_endpoint="", oss_access_key_id=SecretStr(_variable(store["access_key_id_env"])),
            oss_access_key_secret=SecretStr(_variable(store["access_key_secret_env"])),
            oss_security_token=SecretStr(_variable(store["security_token_env"], required=False)),
            upload_max_bytes=MAX_OBJECT_BYTES, pdf_max_bytes=MAX_OBJECT_BYTES)
        storage = OSSGCStorage(cfg)
    backend_path = str(Path(__file__).resolve().parents[1] / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    return FileCollector(storage, state, DatabaseReferences(_variable(database_url_env)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="文件引用保留与手工安全清理；默认仅预演")
    parser.add_argument("--config", required=True, help="私密 JSON 配置路径；只含路径和环境变量名")
    sub = parser.add_subparsers(dest="command")
    plan = sub.add_parser("plan"); plan.add_argument("--grace-days", type=int, default=MIN_GRACE_DAYS)
    apply = sub.add_parser("apply"); apply.add_argument("--plan", required=True); apply.add_argument("--digest", required=True)
    restore = sub.add_parser("restore"); restore.add_argument("--bundle", required=True); restore.add_argument("--digest", required=True)
    status = sub.add_parser("status"); status.add_argument("--bundle", required=True)
    for command in (apply, restore):
        command.add_argument("--maintenance-confirmed", action="store_true")
        command.add_argument("--writers-stopped", action="store_true", help="确认 API/Worker/CLI/外部写入与维护工具均已停止")
    args = parser.parse_args(argv)
    try:
        collector = configured(args.config)
        if args.command in {None, "plan"}:
            result = collector.plan(grace_days=getattr(args, "grace_days", MIN_GRACE_DAYS))
        elif args.command == "apply":
            result = collector.apply(args.plan, args.digest, maintenance_confirmed=args.maintenance_confirmed, writers_stopped=args.writers_stopped)
        elif args.command == "restore":
            result = collector.restore(args.bundle, args.digest, maintenance_confirmed=args.maintenance_confirmed, writers_stopped=args.writers_stopped)
        else:
            result = collector.status(args.bundle)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] not in {"stopped", "restore_stopped"} else 1
    except FileGCError as error:
        print(json.dumps({"status": "failed", "code": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "code": "gc_operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
