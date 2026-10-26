"""GC storage safety tests: temporary local files and a stateful fake OSS only."""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
from scripts import file_gc_support as support
from scripts.file_gc_common import FileGCError, ObjectRecord

HAS_OSS = importlib.util.find_spec("alibabacloud_oss_v2") is not None
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
DATA = b"private synthetic fixture"
DIGEST = hashlib.sha256(DATA).hexdigest()


class ErrorAssertions:
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(FileGCError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)


class LocalGCTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.storage = self.root / "storage"
        (self.storage / "owner").mkdir(parents=True)
        self.source = self.storage / "owner" / "original"
        self.source.write_bytes(DATA)
        self.payload = self.root / "archive"
        self.payload.write_bytes(DATA)
        self.destination = self.root / "export"
        self.store = support.LocalGCStorage(self.storage)

    def test_inventory_is_complete_sorted_and_keeps_unknown_names(self):
        (self.storage / "unknown.tmp").write_bytes(b"unknown")
        records = self.store.inventory()
        self.assertEqual([r.key for r in records], ["owner/original", "unknown.tmp"])
        self.assertEqual(records[0], self.store.head("owner/original"))
        self.assertEqual(self.store.backend_identity(), {"backend": "local", "root": str(self.storage.resolve())})

    def test_inventory_limit_and_io_failure_fail_closed(self):
        (self.storage / "other").write_bytes(b"x")
        self.assert_code("gc_inventory_limit", self.store.inventory, 1)
        with patch.object(support.os, "scandir", side_effect=PermissionError("private-root")):
            self.assert_code("gc_inventory_incomplete", self.store.inventory)

    def test_symlink_and_hardlink_are_rejected(self):
        link = self.storage / "alias"
        try:
            link.symlink_to(self.source)
        except OSError:
            pass
        else:
            self.assert_code("gc_storage_path_invalid", self.store.inventory)
            link.unlink()
        os.link(self.source, link)
        self.assert_code("gc_storage_path_invalid", self.store.inventory)
        self.assert_code("gc_storage_path_invalid", self.store.head, "owner/original")

    def test_reparse_point_and_abnormal_file_are_rejected(self):
        original = Path.lstat

        def replaced(path):
            info = original(path)
            if path == self.source:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, "lstat", replaced):
            self.assert_code("gc_storage_path_invalid", self.store.head, "owner/original")
        fake = SimpleNamespace(st_mode=stat.S_IFIFO, st_nlink=1)
        self.assert_code("gc_storage_path_invalid", support._local_record, "owner/original", fake)

    def test_invalid_keys_and_root_alias_are_rejected(self):
        for key in ("../other", "owner/../other", "owner\\original", "owner/a.", "CON/original", "/owner/original"):
            with self.subTest(key=key):
                self.assert_code("gc_object_key_invalid", self.store.head, key)
        self.assert_code("gc_storage_path_invalid", support.LocalGCStorage, self.storage / ".." / "storage")

    def test_missing_has_fixed_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "^object_not_found$"):
            self.store.head("owner/missing")

    def test_export_is_private_exclusive_and_hashed(self):
        result = self.store.export(self.store.head("owner/original"), self.destination)
        self.assertEqual(result, {"size": len(DATA), "sha256": DIGEST})
        self.assertEqual(self.destination.read_bytes(), DATA)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        self.assert_code("gc_export_failed", self.store.export, self.store.head("owner/original"), self.destination)
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_export_identity_drift_and_copy_failure_leave_no_payload(self):
        record = self.store.head("owner/original")
        self.source.write_bytes(b"changed")
        self.assert_code("gc_object_changed", self.store.export, record, self.destination)
        self.assertFalse(self.destination.exists())
        self.source.write_bytes(DATA)
        record = self.store.head("owner/original")
        capture = support._capture

        def changed(*args):
            result = capture(*args)
            self.source.write_bytes(b"changed-after-copy")
            return result

        with patch.object(support, "_capture", side_effect=changed):
            self.assert_code("gc_object_changed", self.store.export, record, self.destination)
        self.assertFalse(self.destination.exists())

    def test_export_bounds_and_missing_parent_do_not_create_payload(self):
        record = self.store.head("owner/original")
        with patch.object(support, "MAX_OBJECT_BYTES", 2):
            self.assert_code("gc_object_too_large", self.store.export, record, self.destination)
        self.assert_code("gc_export_failed", self.store.export, record, self.root / "absent" / "export")
        self.assertFalse(self.destination.exists())

    def test_delete_rechecks_identity(self):
        record = self.store.head("owner/original")
        self.source.write_bytes(b"new")
        self.assert_code("gc_object_changed", self.store.delete, record)
        self.assertEqual(self.source.read_bytes(), b"new")
        receipt = self.store.delete(self.store.head("owner/original"))
        self.assertEqual(receipt, {"backend": "local", "status": "deleted", "key": "owner/original"})
        self.assertFalse(self.source.exists())

    def test_ensure_writable_leaves_no_probe(self):
        self.store.ensure_writable()
        self.assertEqual([r.key for r in self.store.inventory()], ["owner/original"])

    def test_restore_missing_atomically_and_preserve_existing(self):
        key = "new-owner/restored"
        result = self.store.restore(key, self.payload, size=len(DATA), sha256=DIGEST)
        self.assertEqual(result["status"], "restored")
        restored = self.storage / key
        self.assertEqual(restored.read_bytes(), DATA)
        self.assertEqual(restored.stat().st_nlink, 1)
        self.assertEqual(self.store.restore(key, self.payload, size=len(DATA), sha256=DIGEST)["status"], "already_present")
        restored.write_bytes(b"newer")
        self.assert_code("gc_restore_conflict", self.store.restore, key, self.payload, size=len(DATA), sha256=DIGEST)
        self.assertEqual(restored.read_bytes(), b"newer")

    def test_restore_concurrent_creator_is_never_overwritten(self):
        target = self.storage / "owner" / "restored"
        publish = support._publish_noreplace

        def concurrent(stage, destination):
            target.write_bytes(b"concurrent")
            return publish(stage, destination)

        with patch.object(support, "_publish_noreplace", side_effect=concurrent):
            self.assert_code("gc_restore_conflict", self.store.restore, "owner/restored", self.payload,
                             size=len(DATA), sha256=DIGEST)
        self.assertEqual(target.read_bytes(), b"concurrent")
        self.assertFalse(list(target.parent.glob(".file-gc-restore-*")))

    def test_restore_corrupt_or_alias_payload_never_publishes(self):
        self.assert_code("gc_archive_invalid", self.store.restore, "owner/restored", self.payload,
                         size=len(DATA), sha256="0" * 64)
        self.assertFalse((self.storage / "owner/restored").exists())
        os.link(self.payload, self.root / "payload-alias")
        self.assert_code("gc_storage_path_invalid", self.store.restore, "owner/restored", self.payload,
                         size=len(DATA), sha256=DIGEST)

    def test_empty_object_can_be_exported_deleted_and_restored(self):
        self.source.write_bytes(b"")
        result = self.store.export(self.store.head("owner/original"), self.destination)
        self.assertEqual(result, {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
        self.store.delete(self.store.head("owner/original"))
        restored = self.store.restore("owner/original", self.destination, **result)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.source.read_bytes(), b"")

    def test_checkpoint_interrupts_local_inventory_and_export(self):
        calls = 0

        def timeout():
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise FileGCError("gc_timeout")

        self.store.checkpoint = timeout
        self.assert_code("gc_timeout", self.store.inventory)
        self.assertEqual(self.source.read_bytes(), DATA)
        self.store.checkpoint = support._noop
        record = self.store.head("owner/original")
        capture = support._capture
        copy_calls = 0

        def timeout_after_write():
            nonlocal copy_calls
            copy_calls += 1
            if copy_calls >= 3:
                raise FileGCError("gc_timeout")

        def chunk_timeout(*args):
            return capture(*args[:3], checkpoint=timeout_after_write)

        with patch.object(support, "_capture", side_effect=chunk_timeout):
            self.assert_code("gc_timeout", self.store.export, record, self.destination)
        self.assertFalse(self.destination.exists())

    def test_export_syncs_payload_then_parent_before_success(self):
        events = []
        fsync = os.fsync

        def file_sync(descriptor):
            events.append("payload_fsync")
            return fsync(descriptor)

        def directory_sync(directory):
            self.assertEqual(directory, self.destination.parent)
            self.assertEqual(self.destination.read_bytes(), DATA)
            events.append("parent_fsync")

        with patch.object(support.os, "fsync", side_effect=file_sync), \
                patch.object(support, "_fsync_directory", side_effect=directory_sync):
            self.store.export(self.store.head("owner/original"), self.destination)
            events.append("returned")
        self.assertEqual(events, ["payload_fsync", "parent_fsync", "returned"])

    def test_archive_parent_sync_failure_cleans_payload_and_keeps_live_object(self):
        with patch.object(support, "_fsync_directory", side_effect=FileGCError("gc_directory_sync_failed")):
            self.assert_code("gc_directory_sync_failed", self.store.export,
                             self.store.head("owner/original"), self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), DATA)

    def test_restore_syncs_after_atomic_publish_before_receipt(self):
        target = self.storage / "owner" / "restored"
        events = []
        fsync, rename = os.fsync, support._publish_noreplace

        def file_sync(descriptor):
            events.append("payload_fsync")
            return fsync(descriptor)

        def publish(*args, **kwargs):
            events.append("published")
            return rename(*args, **kwargs)

        def directory_sync(directory):
            self.assertEqual(directory, target.parent)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertFalse(list(directory.glob(".file-gc-restore-*")))
            events.append("parent_fsync")

        with patch.object(support.os, "fsync", side_effect=file_sync), \
                patch.object(support, "_publish_noreplace", side_effect=publish), \
                patch.object(support, "_fsync_directory", side_effect=directory_sync):
            result = self.store.restore("owner/restored", self.payload, size=len(DATA), sha256=DIGEST)
            events.append("returned")
        self.assertEqual(result["status"], "restored")
        self.assertEqual(events, ["payload_fsync", "published", "parent_fsync", "returned"])

    def test_restore_sync_failure_preserves_archive_and_complete_target(self):
        with patch.object(support, "_fsync_directory", side_effect=FileGCError("gc_directory_sync_failed")):
            self.assert_code("gc_directory_sync_failed", self.store.restore, "owner/restored", self.payload,
                             size=len(DATA), sha256=DIGEST)
        self.assertEqual(self.payload.read_bytes(), DATA)
        self.assertEqual((self.storage / "owner/restored").read_bytes(), DATA)
        self.assertFalse(list(self.storage.rglob(".file-gc-restore-*")))

    def test_posix_directory_sync_checks_identity_and_flushes_descriptor(self):
        info = self.root.lstat()
        with patch.object(support, "_WINDOWS", False), \
                patch.object(support.os, "open", return_value=123456) as opened, \
                patch.object(support.os, "fstat", return_value=info), \
                patch.object(support.os, "fsync") as synced, \
                patch.object(support.os, "close") as closed:
            support._fsync_directory(self.root)
        self.assertEqual(opened.call_args.args[0], self.root)
        synced.assert_called_once_with(123456)
        closed.assert_called_once_with(123456)

    def test_directory_sync_failure_is_sanitized_and_windows_does_not_claim_flush(self):
        info = self.root.lstat()
        with patch.object(support, "_WINDOWS", False), \
                patch.object(support.os, "open", return_value=123456), \
                patch.object(support.os, "fstat", return_value=info), \
                patch.object(support.os, "fsync", side_effect=OSError("sensitive-path")), \
                patch.object(support.os, "close") as closed:
            self.assert_code("gc_directory_sync_failed", support._fsync_directory, self.root)
        closed.assert_called_once_with(123456)
        with patch.object(support, "_WINDOWS", True), patch.object(support.os, "open") as opened:
            support._fsync_directory(self.root)
        opened.assert_not_called()

    def test_crash_immediately_after_publish_leaves_single_link_and_retry_works(self):
        script = """
import os, sys
from pathlib import Path
from scripts import file_gc_support as support
publish = support._publish_noreplace
def crash_after_publish(stage, target):
    publish(stage, target)
    os._exit(73)
support._publish_noreplace = crash_after_publish
support.LocalGCStorage(sys.argv[1]).restore('owner/restored', Path(sys.argv[2]),
                                          size=int(sys.argv[3]), sha256=sys.argv[4])
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.storage), str(self.payload),
                                 str(len(DATA)), DIGEST], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", errors="replace"))
        target = self.storage / "owner/restored"
        self.assertEqual(target.read_bytes(), DATA)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(list(self.storage.rglob(".file-gc-restore-*")))
        self.assertEqual(self.store.restore("owner/restored", self.payload,
                                           size=len(DATA), sha256=DIGEST)["status"], "already_present")
        self.assertEqual([item.key for item in self.store.inventory()], ["owner/original", "owner/restored"])

    def test_unsupported_atomic_publish_never_uses_hardlink_fallback(self):
        with patch.object(support, "_WINDOWS", False), \
                patch.object(support.sys, "platform", "unsupported"), patch.object(support.os, "link") as linked:
            self.assert_code("gc_atomic_publish_unsupported", self.store.restore,
                             "owner/restored", self.payload, size=len(DATA), sha256=DIGEST)
        linked.assert_not_called()
        self.assertFalse((self.storage / "owner/restored").exists())


class FakeBody:
    def __init__(self, data):
        self.data, self.closed = data, False

    def iter_bytes(self, **kwargs):
        yield self.data

    def close(self):
        self.closed = True


class FakeOSS:
    def __init__(self):
        self.objects = {"private/owner/original": [self.data_version("original-version")]}
        self.calls, self.bodies = [], []
        self.versioning = "Enabled"
        self.object_pages, self.version_pages = None, None
        self.before_delete = None
        self.marker_number = 0

    @staticmethod
    def data_version(identifier, data=DATA, stamp=STAMP):
        return {"version_id": identifier, "data": data, "last_modified": stamp,
                "etag": '"fixture-etag"', "marker": False}

    def missing(self, code="NoSuchKey"):
        from alibabacloud_oss_v2.exceptions import ServiceError
        raise ServiceError(status_code=404, code=code, message="secret signed-url", request_id="fixture",
                           ec="fixture", timestamp="fixture", request_target="fixture")

    def selected(self, request):
        versions = self.objects.get(request.key, [])
        if request.version_id is None:
            if not versions or versions[0]["marker"]:
                self.missing()
            return versions[0]
        selected = next((v for v in versions if v["version_id"] == request.version_id), None)
        if selected is None or selected["marker"]:
            self.missing("NoSuchVersion")
        return selected

    def metadata(self, version):
        return SimpleNamespace(content_length=len(version["data"]), etag=version["etag"],
                               version_id=version["version_id"], last_modified=version["last_modified"])

    def head_object(self, request, **kwargs):
        self.calls.append(("head", request, kwargs))
        return self.metadata(self.selected(request))

    def get_object(self, request, **kwargs):
        self.calls.append(("get", request, kwargs))
        version = self.selected(request)
        result = self.metadata(version)
        result.body = FakeBody(version["data"])
        self.bodies.append(result.body)
        return result

    def get_bucket_versioning(self, request, **kwargs):
        self.calls.append(("versioning", request, kwargs))
        return SimpleNamespace(version_status=self.versioning)

    def list_objects_v2(self, request, **kwargs):
        self.calls.append(("list", request, kwargs))
        if self.object_pages is not None:
            return self.object_pages.pop(0)
        contents = []
        for key, versions in self.objects.items():
            if versions and not versions[0]["marker"] and key.startswith(request.prefix):
                value = self.metadata(versions[0])
                value.key, value.size = key, value.content_length
                contents.append(value)
        return SimpleNamespace(contents=contents, is_truncated=False, common_prefixes=[])

    def list_object_versions(self, request, **kwargs):
        self.calls.append(("versions", request, kwargs))
        if self.version_pages is not None:
            return self.version_pages.pop(0)
        data, markers = [], []
        for key, versions in self.objects.items():
            if key.startswith(request.prefix):
                for number, version in enumerate(versions):
                    item = SimpleNamespace(key=key, version_id=version["version_id"], is_latest=number == 0,
                                           last_modified=version["last_modified"])
                    if version["marker"]:
                        markers.append(item)
                    else:
                        item.size, item.etag = len(version["data"]), version["etag"]
                        data.append(item)
        return SimpleNamespace(version=data, delete_marker=markers, is_truncated=False, common_prefixes=[])

    def delete_object(self, request, **kwargs):
        self.calls.append(("delete", request, kwargs))
        if self.before_delete is not None:
            self.before_delete(request)
        versions = self.objects.setdefault(request.key, [])
        if request.version_id is None:
            self.marker_number += 1
            marker = {"version_id": "marker-" + str(self.marker_number), "marker": True,
                      "last_modified": STAMP + timedelta(days=10)}
            versions.insert(0, marker)
            return SimpleNamespace(delete_marker=True, version_id=marker["version_id"])
        selected = next((v for v in versions if v["version_id"] == request.version_id), None)
        if selected is None:
            self.missing("NoSuchVersion")
        versions.remove(selected)
        return SimpleNamespace(delete_marker=selected["marker"], version_id=request.version_id)


@unittest.skipUnless(HAS_OSS, "OSS SDK is installed with backend dependencies")
class OSSGCTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = self.root / "archive"
        self.payload.write_bytes(DATA)
        self.client = FakeOSS()
        self.cfg = SimpleNamespace(oss_bucket="fixture-bucket", oss_prefix="private/", oss_region="cn-hangzhou")
        self.store = support.OSSGCStorage(self.cfg, client=self.client)
        self.key = "owner/original"

    def deleted(self):
        record = self.store.head(self.key)
        return record, self.store.delete(record)

    def restore(self, original, receipt):
        return self.store.restore(self.key, self.payload, size=len(DATA), sha256=DIGEST,
                                  original=original, receipt=receipt)

    def test_identity_inventory_and_head_capture_version(self):
        self.assertEqual(self.store.backend_identity(), {"backend": "oss", "bucket": "fixture-bucket",
                                                        "prefix": "private/", "region": "cn-hangzhou"})
        records = self.store.inventory()
        self.assertEqual(records, [self.store.head(self.key)])
        self.assertEqual(records[0].identity, {"etag": '"fixture-etag"', "version_id": "original-version"})

    def test_inventory_pagination_is_complete_and_prefix_limited(self):
        item = SimpleNamespace(key="private/unknown name", size=3, etag='"etag"', last_modified=STAMP)
        self.client.object_pages = [SimpleNamespace(contents=[item], is_truncated=True,
                                                    next_continuation_token="page-2"),
                                    SimpleNamespace(contents=[], is_truncated=False)]
        result = self.store.inventory()
        self.assertEqual(result[0].key, "unknown name")
        lists = [call[1] for call in self.client.calls if call[0] == "list"]
        self.assertEqual([(r.prefix, r.continuation_token, r.delimiter) for r in lists],
                         [("private/", None, None), ("private/", "page-2", None)])
        self.assertFalse(any(call[0] == "head" for call in self.client.calls))

    def test_inventory_repeated_tokens_duplicates_outside_prefix_and_limits_fail_closed(self):
        repeated = SimpleNamespace(contents=[], is_truncated=True, next_continuation_token="repeat")
        self.client.object_pages = [repeated, repeated]
        self.assert_code("gc_inventory_incomplete", self.store.inventory)
        item = SimpleNamespace(key="elsewhere/object", size=1, etag='"etag"', last_modified=STAMP)
        self.client.object_pages = [SimpleNamespace(contents=[item], is_truncated=False)]
        self.assert_code("gc_inventory_incomplete", self.store.inventory)
        item.key = "private/unknown name"
        self.client.object_pages = [SimpleNamespace(contents=[item, item], is_truncated=False)]
        self.assert_code("gc_inventory_incomplete", self.store.inventory)
        item2 = SimpleNamespace(key="private/another name", size=1, etag='"etag"', last_modified=STAMP)
        self.client.object_pages = [SimpleNamespace(contents=[item, item2], is_truncated=False)]
        self.assert_code("gc_inventory_limit", self.store.inventory, 1)

    def test_export_pins_original_version_and_closes_body(self):
        record = self.store.head(self.key)
        target = self.root / "export"
        self.assertEqual(self.store.export(record, target), {"size": len(DATA), "sha256": DIGEST})
        request = next(call[1] for call in self.client.calls if call[0] == "get")
        self.assertEqual((request.key, request.version_id, request.if_match),
                         ("private/owner/original", "original-version", None))
        self.assertTrue(self.client.bodies[0].closed)

    def test_unversioned_export_uses_if_match(self):
        self.client.objects["private/owner/original"][0]["version_id"] = None
        self.store.export(self.store.head(self.key), self.root / "export")
        request = next(call[1] for call in self.client.calls if call[0] == "get")
        self.assertIsNone(request.version_id)
        self.assertEqual(request.if_match, '"fixture-etag"')

    def test_export_drift_retains_existing_archive_and_sanitizes_failure(self):
        record = self.store.head(self.key)
        self.client.objects["private/owner/original"][0]["etag"] = '"changed"'
        self.assert_code("gc_object_changed", self.store.export, record, self.root / "export")
        with patch.object(self.client, "head_object", side_effect=RuntimeError("credential signed-url")):
            self.assert_code("gc_storage_unavailable", self.store.head, self.key)

    def test_only_enabled_bucket_is_writable(self):
        for value in (None, "Suspended", "Unknown"):
            self.client.versioning = value
            self.assert_code("gc_oss_versioning_required", self.store.ensure_writable)
        self.assertFalse(any(call[0] == "delete" for call in self.client.calls))

    def test_delete_creates_marker_records_original_and_disables_retry(self):
        original, receipt = self.deleted()
        request, kwargs = [(c[1], c[2]) for c in self.client.calls if c[0] == "delete"][0]
        self.assertIsNone(request.version_id)
        self.assertEqual(kwargs, {"retry_max_attempts": 0})
        self.assertEqual(receipt, {"backend": "oss", "status": "deleted", "key": self.key,
                                   "delete_marker": True, "marker_version_id": "marker-1",
                                   "original_version_id": original.identity["version_id"]})
        self.assertEqual(self.client.objects["private/owner/original"][1]["data"], DATA)

    def test_delete_unknown_acknowledgement_is_not_retried(self):
        original = self.store.head(self.key)
        with patch.object(self.client, "delete_object", side_effect=RuntimeError("signed-url")) as delete:
            self.assert_code("gc_delete_unknown", self.store.delete, original)
        self.assertEqual(delete.call_count, 1)
        with patch.object(self.client, "delete_object", return_value=SimpleNamespace(delete_marker=False, version_id="data")):
            self.assert_code("gc_delete_unknown", self.store.delete, original)

    def test_restore_removes_only_proven_own_marker_without_put(self):
        original, receipt = self.deleted()
        result = self.restore(original, receipt)
        self.assertEqual(result, {"backend": "oss", "status": "restored", "key": self.key,
                                  "removed_marker_version_id": "marker-1"})
        deletes = [(c[1], c[2]) for c in self.client.calls if c[0] == "delete"]
        self.assertEqual([request.version_id for request, _ in deletes], [None, "marker-1"])
        self.assertEqual(deletes[-1][1], {"retry_max_attempts": 0})
        self.assertEqual(self.store.head(self.key), original)
        self.assertEqual(self.restore(original, receipt)["status"], "already_present")

    def test_restore_unknown_receipt_requires_manual_recovery(self):
        original, receipt = self.deleted()
        receipt["status"] = "unknown"
        self.assertEqual(self.restore(original, receipt)["status"], "manual_required")
        self.assertEqual(len([c for c in self.client.calls if c[0] == "delete"]), 1)

    def test_restore_never_deletes_data_version_or_unrelated_marker(self):
        original, receipt = self.deleted()
        receipt["marker_version_id"] = original.identity["version_id"]
        self.assert_code("gc_restore_marker_changed", self.restore, original, receipt)
        receipt["marker_version_id"] = "someone-elses-marker"
        self.assert_code("gc_restore_marker_changed", self.restore, original, receipt)
        self.assertEqual(len([c for c in self.client.calls if c[0] == "delete"]), 1)

    def test_restore_corrupt_archive_or_original_version_is_never_deleted(self):
        original, receipt = self.deleted()
        self.payload.write_bytes(b"x" * len(DATA))
        self.assert_code("gc_archive_invalid", self.restore, original, receipt)
        self.payload.write_bytes(DATA)
        self.client.objects["private/owner/original"][1]["data"] = b"x" * len(DATA)
        self.assert_code("gc_archive_invalid", self.restore, original, receipt)
        self.assertEqual(len([c for c in self.client.calls if c[0] == "delete"]), 1)

    def test_restore_existing_new_data_is_preserved(self):
        original, receipt = self.deleted()
        self.client.objects["private/owner/original"].insert(0, FakeOSS.data_version("new-version", b"newer"))
        self.assert_code("gc_restore_conflict", self.restore, original, receipt)
        self.assertEqual(self.client.objects["private/owner/original"][0]["data"], b"newer")

    def test_restore_concurrent_put_is_not_overwritten(self):
        original, receipt = self.deleted()

        def concurrent(request):
            if request.version_id is not None:
                self.client.objects[request.key].insert(0, FakeOSS.data_version("new-version", b"newer"))

        self.client.before_delete = concurrent
        self.assertEqual(self.restore(original, receipt)["status"], "restored")
        self.assertEqual(self.client.objects["private/owner/original"][0]["data"], b"newer")
        self.assertEqual(self.client.objects["private/owner/original"][1]["version_id"], "original-version")

    def test_restore_version_pagination_is_bounded_and_cycle_is_rejected(self):
        original, receipt = self.deleted()
        page = SimpleNamespace(version=[], delete_marker=[], is_truncated=True,
                               next_key_marker="private/owner/original", next_version_id_marker="repeat")
        self.client.version_pages = [page, page]
        self.assert_code("gc_restore_versions_invalid", self.restore, original, receipt)
        self.assertEqual(len([c for c in self.client.calls if c[0] == "delete"]), 1)

    def test_restore_real_version_pagination_filters_exact_key(self):
        original, receipt = self.deleted()
        full = self.client.list_object_versions(SimpleNamespace(prefix="private/owner/original"))
        unrelated = SimpleNamespace(key="private/owner/original-extra", version_id="unrelated", is_latest=True)
        self.client.version_pages = [SimpleNamespace(version=[], delete_marker=full.delete_marker, is_truncated=True,
                                                     next_key_marker="private/owner/original", next_version_id_marker="marker-1"),
                                     SimpleNamespace(version=full.version + [unrelated], delete_marker=[], is_truncated=False)]
        self.assertEqual(self.restore(original, receipt)["status"], "restored")
        requests = [c[1] for c in self.client.calls if c[0] == "versions"]
        self.assertEqual(requests[-1].version_id_marker, "marker-1")

    def test_restore_rejects_later_foreign_marker_and_missing_original(self):
        original, receipt = self.deleted()
        foreign = {"version_id": "foreign-marker", "marker": True, "last_modified": STAMP + timedelta(days=1)}
        self.client.objects["private/owner/original"].insert(1, foreign)
        self.assert_code("gc_restore_versions_changed", self.restore, original, receipt)
        self.client.objects["private/owner/original"] = self.client.objects["private/owner/original"][:1]
        self.assert_code("gc_restore_original_missing", self.restore, original, receipt)

    def test_empty_object_is_reversibly_deleted(self):
        self.payload.write_bytes(b"")
        self.client.objects["private/owner/original"][0]["data"] = b""
        original, receipt = self.deleted()
        result = self.store.restore(self.key, self.payload, size=0, sha256=hashlib.sha256(b"").hexdigest(),
                                    original=original, receipt=receipt)
        self.assertEqual(result["status"], "restored")

    def test_checkpoint_interrupts_inventory_before_second_page(self):
        self.client.object_pages = [SimpleNamespace(contents=[], is_truncated=True,
                                                    next_continuation_token="page-2"),
                                    SimpleNamespace(contents=[], is_truncated=False)]

        def timeout_after_request():
            if any(call[0] == "list" for call in self.client.calls):
                raise FileGCError("gc_timeout")

        self.store.checkpoint = timeout_after_request
        self.assert_code("gc_timeout", self.store.inventory)
        self.assertEqual(len(self.client.object_pages), 1)
        self.assertFalse(any(call[0] == "delete" for call in self.client.calls))

    def test_checkpoint_closes_stream_after_get_and_keeps_known_delete_receipt(self):
        original = self.store.head(self.key)

        def timeout_after_get():
            if any(call[0] == "get" for call in self.client.calls):
                raise FileGCError("gc_timeout")

        self.store.checkpoint = timeout_after_get
        self.assert_code("gc_timeout", self.store.export, original, self.root / "export")
        self.assertTrue(self.client.bodies[0].closed)
        self.assertFalse((self.root / "export").exists())
        expired = False

        def timeout():
            if expired:
                raise FileGCError("gc_timeout")

        def expire_on_delete(request):
            nonlocal expired
            expired = True

        self.store.checkpoint = timeout
        self.client.before_delete = expire_on_delete
        self.assertEqual(self.store.delete(original)["status"], "deleted")


if __name__ == "__main__":
    unittest.main()
