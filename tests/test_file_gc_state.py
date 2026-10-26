"""Isolated filesystem tests for operator-only GC state and recovery bundles."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import file_gc_state as state
from scripts.file_gc_common import FileGCError


class StateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = state.PrivateState(self.base / "private")

    def assert_code(self, code, function, *args):
        with self.assertRaises(FileGCError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn("SENSITIVE_SENTINEL", str(caught.exception))

    def test_private_roundtrip_replacement_and_safe_names(self):
        self.store.write("plan.json", {"version": 1, "text": "隔离材料"})
        self.assertEqual(self.store.read("plan.json"), {"version": 1, "text": "隔离材料"})
        self.store.write("plan.json", {"version": 2})
        self.assertEqual(self.store.read("plan.json"), {"version": 2})
        self.assertEqual({path.name for path in self.store.root.iterdir()}, {"plan.json"})
        for name in ("", ".", "..", "../escape", "folder/plan.json", r"folder\plan.json",
                     "plan.json ", "plan.json.", "plan:stream", "CON", "nul.json", "gc.lock",
                     "x" * 129, "SENSITIVE_SENTINEL\0"):
            self.assert_code("gc_path_invalid", self.store.write, name, {})
        self.assertEqual({path.name for path in self.store.root.iterdir()}, {"plan.json"})

    def test_json_is_bounded_strict_and_requires_an_object(self):
        path = self.store.root / "plan.json"
        for raw in (b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}',
                    b'{"x":Infinity}', b'{"x":1e9999}', b'[]', b'null', b'1', b'{} {}', b'\xff'):
            self.store.write("plan.json", {})
            path.write_bytes(raw)
            self.assert_code("gc_state_invalid", self.store.read, "plan.json")
        for document in ([], None, {"nan": float("nan")}, {"infinity": float("inf")},
                         {1: "first", "1": "duplicate"}):
            self.assert_code("gc_state_invalid", self.store.write, "other.json", document)
        self.store.write("plan.json", {"x": "long data"})
        with patch.object(state, "MAX_DOCUMENT_BYTES", 8):
            self.assert_code("gc_state_invalid", self.store.read, "plan.json")
            self.assert_code("gc_state_invalid", self.store.write, "other.json", {"long": "value"})

    def test_atomic_write_fsync_and_replace_failures_preserve_previous_state(self):
        self.store.write("plan.json", {"old": True})
        for operation in ("fsync", "replace"):
            with patch.object(state.os, operation, side_effect=OSError("SENSITIVE_SENTINEL")):
                self.assert_code("gc_state_unavailable", self.store.write, "plan.json", {"new": True})
            self.assertEqual(self.store.read("plan.json"), {"old": True})
            self.assertEqual({path.name for path in self.store.root.iterdir()}, {"plan.json"})

    def test_atomic_write_rejects_destination_replaced_during_fsync(self):
        self.store.write("plan.json", {"old": True})
        fsync = state.os.fsync
        mutated = False

        def change(descriptor):
            nonlocal mutated
            if not mutated:
                mutated = True
                replacement = self.store.root / "concurrent.json"
                replacement.write_bytes(b'{"concurrent":true}')
                replacement.chmod(0o600)
                os.replace(replacement, self.store.root / "plan.json")
            return fsync(descriptor)

        with patch.object(state.os, "fsync", side_effect=change):
            self.assert_code("gc_state_invalid", self.store.write, "plan.json", {"new": True})
        self.assertEqual(self.store.read("plan.json"), {"concurrent": True})
        self.assertEqual({path.name for path in self.store.root.iterdir()}, {"plan.json"})

    def test_missing_parent_and_leaf_have_controlled_errors(self):
        self.assert_code("gc_state_unavailable", state.PrivateState, self.base / "absent" / "private")
        self.assert_code("gc_state_unavailable", self.store.read, "absent.json")

    def test_hardlinks_are_rejected_without_overwriting_outside_data(self):
        outside = self.base / "outside.json"
        outside.write_bytes(b'{"private":"SENSITIVE_SENTINEL"}')
        outside.chmod(0o600)
        os.link(outside, self.store.root / "plan.json")
        self.assert_code("gc_state_invalid", self.store.read, "plan.json")
        self.assert_code("gc_state_invalid", self.store.write, "plan.json", {})
        self.assertEqual(outside.read_bytes(), b'{"private":"SENSITIVE_SENTINEL"}')

    def test_symlink_root_parent_and_leaf_are_rejected(self):
        link = self.base / "link"
        try:
            link.symlink_to(self.store.root, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit creating symlinks")
        self.assert_code("gc_path_invalid", state.PrivateState, link)
        self.assert_code("gc_path_invalid", state.PrivateState, link / "child")
        leaf = self.store.root / "linked.json"
        leaf.symlink_to(self.base / "outside")
        self.assert_code("gc_path_invalid", self.store.write, "linked.json", {})

    def test_raw_aliases_and_reparse_points_are_rejected(self):
        for root in (str(self.base) + "/../private", str(self.base) + "/./private",
                     self.base / "private.", self.base / "NUL.json"):
            self.assert_code("gc_path_invalid", state.PrivateState, root)
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == self.store.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            self.assert_code("gc_path_invalid", self.store.read, "plan.json")

    def test_replaced_state_root_is_rejected(self):
        saved = self.base / "saved"
        self.store.root.rename(saved)
        self.store.root.mkdir(mode=0o700)
        self.assert_code("gc_path_invalid", self.store.write, "plan.json", {})
        self.assertTrue(saved.is_dir())

    def test_lock_is_stable_exclusive_and_released_after_process_crash(self):
        code = ("import sys; from scripts.file_gc_state import PrivateState; from scripts.file_gc_common import FileGCError; "
                "s=PrivateState(sys.argv[1]); "
                "\ntry:\n with s.lock(): print('acquired', flush=True); sys.stdin.read()"
                "\nexcept FileGCError as e: print(e.code, flush=True)")
        environment = {**os.environ, "PYTHONPATH": str(ROOT)}
        with self.store.lock():
            identity = (self.store.root / "gc.lock").stat().st_ino
            result = subprocess.run([sys.executable, "-c", code, str(self.store.root)], input="", text=True,
                                    capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.stdout.strip(), "gc_locked", result.stderr)
            self.assert_code("gc_locked", lambda: self.store.lock().__enter__())
        child = subprocess.Popen([sys.executable, "-c", code, str(self.store.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(child.stdout.readline().strip(), "acquired")
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        with self.store.lock():
            self.assertEqual((self.store.root / "gc.lock").stat().st_ino, identity)
        # Windows byte-range locks also deny a second handle's read of byte 0.
        self.assertEqual((self.store.root / "gc.lock").read_bytes(), b"\0")

    def test_incomplete_lock_creation_is_recoverable_and_hardlinked_lock_is_rejected(self):
        lock = self.store.root / "gc.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        with self.store.lock():
            self.assertEqual(lock.stat().st_size, 1)
        outside = self.base / "outside.lock"
        os.link(lock, outside)
        self.assert_code("gc_state_invalid", lambda: self.store.lock().__enter__())
        self.assertEqual(outside.read_bytes(), b"\0")

    def test_bundle_creation_is_exclusive_and_does_not_escape(self):
        bundle = self.store.create_bundle("run-0123456789abcdef")
        self.assertEqual(bundle.parent, self.store.root)
        self.assertEqual(self.store.bundle(bundle.name), bundle)
        self.assertEqual({path.name for path in bundle.iterdir()}, {"objects"})
        self.assert_code("gc_state_unavailable", self.store.create_bundle, bundle.name)
        self.assert_code("gc_path_invalid", self.store.create_bundle, "../outside")
        self.assert_code("gc_path_invalid", self.store.bundle, "objects/elsewhere")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_posix_permissions_are_private_and_broad_existing_paths_are_rejected(self):
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.store.write("plan.json", {})
        self.assertEqual(stat.S_IMODE((self.store.root / "plan.json").stat().st_mode), 0o600)
        bundle = self.store.create_bundle("run-one")
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((bundle / "objects").stat().st_mode), 0o700)
        (self.store.root / "plan.json").chmod(0o644)
        self.assert_code("gc_state_invalid", self.store.read, "plan.json")
        self.store.root.chmod(0o755)
        self.assert_code("gc_path_invalid", state.PrivateState, self.store.root)

    @unittest.skipUnless(os.name == "nt", "Windows ACL validation")
    def test_windows_private_acl_is_verified(self):
        self.assertTrue(state._windows_private(self.store.root))
        self.store.write("plan.json", {})
        self.assertTrue(state._windows_private(self.store.root / "plan.json"))
        with patch.object(state, "_windows_private", return_value=False):
            self.assert_code("gc_path_invalid", state.PrivateState, self.store.root)


class BundleTests(unittest.TestCase):
    assert_code = StateTests.assert_code
    setUp = StateTests.setUp

    def bundle(self):
        root = self.store.create_bundle("run-recovery")
        raw = b"recoverable object\0\xff"
        payload = "objects/000000.bin"
        path = root / payload
        path.write_bytes(raw)
        path.chmod(0o600)
        manifest = {"format": "file-gc", "version": 1, "backend": "fixture",
                    "objects": [{"payload": payload, "size": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(),
                                 "record": {"key": "private/opaque-key"}, "status": "archived", "receipt": None}]}
        state.PrivateState(root).write("manifest.json", manifest)
        return root, manifest

    def test_recovery_bundle_validates_all_bytes_and_accepts_business_fields(self):
        root, manifest = self.bundle()
        self.assertIsNone(state.validate_bundle(root, manifest))
        self.assertEqual(state.PrivateState(root).read("manifest.json"), manifest)
        self.assertEqual({path.name for path in root.iterdir()}, {"manifest.json", "objects"})

    def test_empty_bundle_is_valid_and_manifest_is_required(self):
        root = self.store.create_bundle("empty")
        manifest = {"objects": []}
        state.PrivateState(root).write("manifest.json", manifest)
        state.validate_bundle(root, manifest)
        (root / "manifest.json").unlink()
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)

    def test_zero_byte_payload_is_valid(self):
        root, manifest = self.bundle()
        (root / "objects/000000.bin").write_bytes(b"")
        manifest["objects"][0].update(size=0, sha256=hashlib.sha256(b"").hexdigest())
        state.PrivateState(root).write("manifest.json", manifest)
        state.validate_bundle(root, manifest)

    def test_stored_manifest_must_be_strict_json_and_match_the_supplied_document(self):
        root, manifest = self.bundle()
        changed = {**manifest, "receipt": "changed"}
        state.PrivateState(root).write("manifest.json", changed)
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        (root / "manifest.json").write_bytes(b'{"objects":[],"objects":[]}')
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, {"objects": []})

    def test_corrupt_hash_wrong_size_missing_and_extra_files_are_rejected(self):
        root, manifest = self.bundle()
        path = root / "objects/000000.bin"
        raw = path.read_bytes()
        for content in (raw + b"unexpected", b"x" * len(raw)):
            path.write_bytes(content)
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        path.write_bytes(raw)
        path.unlink()
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        path.write_bytes(raw)
        path.chmod(0o600)
        for extra in (root / "SENSITIVE_SENTINEL", root / "objects/000001.bin"):
            extra.write_bytes(b"unexpected")
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
            extra.unlink()

    def test_duplicate_traversal_invalid_types_hashes_and_limits_are_rejected(self):
        root, manifest = self.bundle()
        duplicate = {**manifest, "objects": manifest["objects"] * 2}
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, duplicate)
        for field, value in (("payload", "../outside"), ("payload", "objects/1.bin"),
                             ("payload", r"objects\000000.bin"), ("size", True), ("size", -1),
                             ("size", "20"), ("size", state.MAX_OBJECT_BYTES + 1),
                             ("sha256", "A" * 64), ("sha256", "0" * 63)):
            changed = copy.deepcopy(manifest)
            changed["objects"][0][field] = value
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, changed)
        for field in ("payload", "size", "sha256"):
            changed = copy.deepcopy(manifest)
            del changed["objects"][0][field]
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, changed)
        with patch.object(state, "MAX_OBJECTS", 0):
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        with patch.object(state, "MAX_TOTAL_BYTES", 1):
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        with patch.object(state, "MAX_DOCUMENT_BYTES", 1):
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)

    def test_payload_hardlinks_and_nonregular_entries_are_rejected(self):
        root, manifest = self.bundle()
        payload = root / "objects/000000.bin"
        outside = self.base / "outside.bin"
        os.link(payload, outside)
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        outside.unlink()
        payload.unlink()
        payload.mkdir(mode=0o700)
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)

    def test_symlinked_payload_and_objects_directory_are_rejected(self):
        root, manifest = self.bundle()
        payload = root / "objects/000000.bin"
        outside = self.base / "outside.bin"
        payload.rename(outside)
        try:
            payload.symlink_to(outside)
        except OSError:
            self.skipTest("OS does not permit creating symlinks")
        self.assert_code("gc_path_invalid", state.validate_bundle, root, manifest)
        payload.unlink()
        (root / "objects").rmdir()
        (root / "objects").symlink_to(self.base, target_is_directory=True)
        self.assert_code("gc_path_invalid", state.validate_bundle, root, manifest)

    def test_bundle_changed_during_verification_is_rejected(self):
        root, manifest = self.bundle()
        hasher = state._hash_file

        def mutate(path, size):
            result = hasher(path, size)
            (root / "SENSITIVE_SENTINEL").write_bytes(b"private")
            return result

        with patch.object(state, "_hash_file", side_effect=mutate):
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)

    def test_read_failures_never_leak_original_exception(self):
        root, manifest = self.bundle()
        with patch.object(state.os, "open", side_effect=OSError("SENSITIVE_SENTINEL")):
            self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_public_payload_or_bundle_is_rejected(self):
        root, manifest = self.bundle()
        (root / "objects/000000.bin").chmod(0o644)
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)
        (root / "objects/000000.bin").chmod(0o600)
        root.chmod(0o755)
        self.assert_code("gc_bundle_invalid", state.validate_bundle, root, manifest)


if __name__ == "__main__":
    unittest.main()
