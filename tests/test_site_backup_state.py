"""Private scheduling state and retention use generated temporary bundles only."""

import hashlib
import json
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
sys.path.insert(0, str(ROOT / "scripts"))
import site_backup_state as state


class StateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = state.StateStore(self.base / "state")

    def assert_code(self, code, function, *args):
        with self.assertRaises(state.ScheduleError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_atomic_roundtrip_and_private_modes(self):
        self.store.write("events/event.json", {"phase": "before", "items": [1, True]})
        self.store.write("events/event.json", {"phase": "after"})
        self.assertEqual(self.store.read("events/event.json"), {"phase": "after"})
        self.assertEqual(self.store.read("absent.json", {"default": True}), {"default": True})
        self.assertEqual(sorted(path.name for path in (self.store.root / "events").iterdir()), ["event.json"])
        self.assertEqual(self.store.root, (self.base / "state").absolute())
        if os.name != "nt":
            for path in (self.store.root, self.store.root / "events"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.store.root / "events/event.json").stat().st_mode), 0o600)

    def test_json_exactly_at_limit_and_deep_safe_relative_paths(self):
        value = {"text": "x" * (state.MAX_STATE_BYTES - len(b'{"text":""}'))}
        self.store.write("events/2026/event.json", value)
        self.assertEqual((self.store.root / "events/2026/event.json").stat().st_size, 65536)
        self.assertEqual(self.store.read("events/2026/event.json"), value)

    def test_error_constructor_rejects_private_diagnostic_text(self):
        error = state.ScheduleError("SENSITIVE_SENTINEL: operator path failed")
        self.assertEqual(error.code, "schedule_failed")
        self.assertEqual(str(error), "schedule_failed")

    def test_failed_replace_keeps_old_value_and_removes_temporary_file(self):
        self.store.write("status.json", {"old": True})
        with patch.object(state.os, "replace", side_effect=OSError("SENSITIVE_SENTINEL")):
            self.assert_code("schedule_state_unavailable", self.store.write, "status.json", {"new": True})
        self.assertEqual(self.store.read("status.json"), {"old": True})
        self.assertEqual([path.name for path in self.store.root.iterdir()], ["status.json"])

    def test_write_flushes_before_atomic_replace(self):
        calls = []
        fsync, replace = state.os.fsync, state.os.replace
        with patch.object(state.os, "fsync", side_effect=lambda fd: (calls.append("fsync"), fsync(fd))[-1]), \
                patch.object(state.os, "replace", side_effect=lambda a, b: (calls.append("replace"), replace(a, b))[-1]):
            self.store.write("status.json", {"ok": True})
        self.assertLess(calls.index("fsync"), calls.index("replace"))

    def test_failed_write_never_cleans_up_a_replaced_temporary_path(self):
        self.store.write("events/status.json", {"old": True})
        impostor = []

        def changed_parent(source, target):
            parent = Path(source).parent
            parent.rename(self.store.root / "original-events")
            parent.mkdir()
            replacement = parent / Path(source).name
            replacement.write_bytes(b"SENSITIVE_SENTINEL")
            impostor.append(replacement)
            raise OSError("SENSITIVE_SENTINEL")

        with patch.object(state.os, "replace", side_effect=changed_parent):
            self.assert_code("schedule_state_unavailable", self.store.write, "events/status.json", {"new": True})
        self.assertEqual(impostor[0].read_bytes(), b"SENSITIVE_SENTINEL")

    def test_bounded_json_duplicate_keys_and_nonfinite_values(self):
        for raw in (b'{"same":1,"same":2}', b'{"nested":{"a":1,"a":2}}', b'{"a":NaN}', b'x' * 65537):
            (self.store.root / "invalid.json").write_bytes(raw)
            self.assert_code("schedule_state_invalid", self.store.read, "invalid.json")
        self.assert_code("schedule_state_invalid", self.store.write, "large.json", {"a": "x" * 65536})
        self.assert_code("schedule_state_invalid", self.store.write, "nan.json", {"a": float("nan")})
        self.assertFalse((self.store.root / "large.json").exists())

    def test_relative_state_paths_reject_traversal_and_windows_aliases(self):
        for name in ("../secret", "a/../secret", "/absolute", "a\\secret", "a//b", "a/./b", "NUL.json", "a./x", "a.json:other", ""):
            with self.subTest(name=name):
                self.assert_code("schedule_path_invalid", self.store.write, name, {})
        self.assert_code("schedule_path_invalid", state.private_path, self.base / "child" / ".." / "outside")

    def test_nonregular_and_hardlinked_state_files_are_rejected(self):
        (self.store.root / "directory.json").mkdir()
        self.assert_code("schedule_state_invalid", self.store.read, "directory.json")
        target = self.base / "outside.json"
        target.write_text('{"sentinel":"SENSITIVE_SENTINEL"}', encoding="utf-8")
        os.link(target, self.store.root / "linked.json")
        self.assert_code("schedule_state_invalid", self.store.read, "linked.json")
        self.assert_code("schedule_state_invalid", self.store.write, "linked.json", {})
        self.assertIn("SENSITIVE_SENTINEL", target.read_text(encoding="utf-8"))

    def test_parent_reparse_attribute_is_rejected(self):
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == self.store.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat):
            self.assert_code("schedule_path_invalid", state.private_path, self.store.root / "status.json")

    def test_symlinked_parent_is_rejected(self):
        link = self.base / "link"
        try:
            link.symlink_to(self.store.root, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit creating symlinks")
        self.assert_code("schedule_path_invalid", state.private_path, link / "status.json")

    def test_lock_is_exclusive_and_stable_across_processes_and_crash(self):
        code = ("import sys; from site_backup_state import StateStore, ScheduleError; "
                "s=StateStore(sys.argv[1]); "
                "\ntry:\n with s.lock(): print('acquired', flush=True); sys.stdin.read()"
                "\nexcept ScheduleError as e: print(e.code, flush=True)")
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
        with self.store.lock():
            before = {path.name: path.stat().st_ino for path in self.store.root.iterdir()}
            result = subprocess.run([sys.executable, "-c", code, str(self.store.root)], input="", text=True,
                                    capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.stdout.strip(), "schedule_locked")
            self.assert_code("schedule_locked", lambda: self.store.lock().__enter__())
        self.assertEqual(before, {path.name: path.stat().st_ino for path in self.store.root.iterdir()})
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
            self.assertEqual(before, {path.name: path.stat().st_ino for path in self.store.root.iterdir()})

    def test_crash_during_initial_lock_creation_does_not_poison_future_lock(self):
        (self.store.root / "schedule.lock").write_bytes(b"")
        with self.store.lock():
            self.assertEqual((self.store.root / "schedule.lock").stat().st_size, 1)
        self.assertEqual((self.store.root / "schedule.lock").read_bytes(), b"\0")

    def test_hardlinked_lock_is_rejected_without_touching_target(self):
        target = self.base / "outside.lock"
        target.write_bytes(b"\0")
        os.link(target, self.store.root / "schedule.lock")
        self.assert_code("schedule_state_invalid", lambda: self.store.lock().__enter__())
        self.assertEqual(target.read_bytes(), b"\0")


class RetentionTests(unittest.TestCase):
    assert_code = StateTests.assert_code

    def setUp(self):
        StateTests.setUp(self)
        self.backups = self.base / "backups"
        self.backups.mkdir()

    def bundle(self, number, *, register=True, revision="current", created_at=None):
        name = f"scheduled-202610{number:02d}T030000Z-{number:012x}"
        root = self.backups / name
        root.mkdir()
        (root / "objects").mkdir()
        payload = b"fixture object"
        relative = "objects/" + hashlib.sha256(b"owner/file").hexdigest()
        (root / relative).write_bytes(payload)
        (root / "database.dump").write_bytes(b"fixture dump")
        manifest = {"format_version": 1, "created_at": created_at or f"2026-10-{number:02d}T03:00:01+00:00",
                    "schema_revision": revision, "postgres_major": 16,
                    "scope": "postgresql_all_public_tables_and_referenced_objects_selected_versions",
                    "source_storage": "local", "tables": {},
                    "dump": {"path": "database.dump", "size": 12,
                             "sha256": hashlib.sha256(b"fixture dump").hexdigest()},
                    "objects": [{"key": "owner/file", "path": relative, "status": "present",
                                 "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                                 "mandatory": True, "references": ["files:00000000-0000-0000-0000-000000000000"],
                                 "version_id": None, "etag": None, "reason": None}]}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if register:
            state.register_backup(self.store, self.backups, name, manifest)
        return name, root, manifest

    def verify(self, root):
        manifest = json.loads((root / "manifest.json").read_bytes())
        if manifest["schema_revision"] != "current":
            raise ValueError("SENSITIVE_SENTINEL old schema")
        for item in [manifest["dump"], *manifest["objects"]]:
            raw = (root / item["path"]).read_bytes()
            if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("SENSITIVE_SENTINEL corrupt")
        return manifest

    def test_registration_records_disk_hash_and_adds_no_bundle_marker(self):
        name, root, manifest = self.bundle(1)
        receipt = self.store.read(f"receipts/{name}.json")
        self.assertEqual(receipt, {"name": name, "created_at": manifest["created_at"],
                                  "schema_revision": "current", "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()})
        self.assertEqual({path.name for path in root.iterdir()}, {"objects", "database.dump", "manifest.json"})
        self.assertEqual(state.register_backup(self.store, self.backups, name, manifest), receipt)
        changed = {**manifest, "schema_revision": "other"}
        self.assert_code("schedule_receipt_invalid", state.register_backup, self.store, self.backups, name, changed)

    def test_strict_names_direct_children_and_separate_roots(self):
        for name in ("scheduled-20260230T030000Z-000000000001", "manual", "../backups", "scheduled-20261001T030000Z-ABCDEF123456"):
            self.assert_code("schedule_receipt_invalid", state.register_backup, self.store, self.backups, name, {})
        self.assert_code("schedule_path_invalid", state.retention_plan, self.store, self.store.root, 1)
        self.assert_code("schedule_path_invalid", state.retention_plan, self.store, self.base, 1)

    def test_plan_is_readonly_stably_sorted_by_utc_and_keeps_boundaries(self):
        third, _, _ = self.bundle(3, created_at="2026-10-02T00:00:00+00:00")
        first, _, _ = self.bundle(1, created_at="2026-10-01T03:00:00+03:00")
        second, _, _ = self.bundle(2, created_at="2026-10-01T00:00:00+00:00")
        before = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(state.retention_plan(self.store, self.backups, 1),
                         {"candidates": [first, second], "retained": [third], "skipped": []})
        self.assertEqual(state.retention_plan(self.store, self.backups, 365)["candidates"], [])
        after = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        for keep in (0, 366, True, "7"):
            self.assert_code("schedule_retention_invalid", state.retention_plan, self.store, self.backups, keep)

    def test_unknown_staging_and_bad_receipts_are_not_candidates(self):
        good, _, _ = self.bundle(1)
        unknown, _, _ = self.bundle(2, register=False)
        bad, _, _ = self.bundle(3)
        (self.store.root / f"receipts/{bad}.json").write_bytes(b'{"name":1,"name":2}')
        for name in ("manual-copy", ".scheduled-staging"):
            (self.backups / name).mkdir()
        plan = state.retention_plan(self.store, self.backups, 1)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["retained"], [good])
        self.assertEqual({item["name"] for item in plan["skipped"]}, {unknown, bad, "unmanaged_backup"})
        self.assertEqual(sum(item["name"] == "unmanaged_backup" for item in plan["skipped"]), 2)

    def test_unknown_private_names_never_escape_retention_result(self):
        (self.backups / "SENSITIVE_SENTINEL-manual-copy").mkdir()
        result = state.retention_plan(self.store, self.backups, 1)
        self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))

    def test_future_receipt_time_is_rejected(self):
        name, _, manifest = self.bundle(1, register=False, created_at="2099-01-01T00:00:00+00:00")
        self.assert_code("schedule_receipt_invalid", state.register_backup, self.store, self.backups, name, manifest)

    def test_root_enumeration_is_bounded(self):
        for number in range(3):
            (self.backups / f"manual-{number}").mkdir()
        with patch.object(state, "MAX_BACKUPS", 2):
            self.assert_code("schedule_retention_invalid", state.retention_plan, self.store, self.backups, 1)

    def test_prune_deletes_only_verified_old_registered_bundles(self):
        first, old, _ = self.bundle(1)
        second, kept, _ = self.bundle(2)
        manual, manual_root, _ = self.bundle(3, register=False)
        result = state.prune_backups(self.store, self.backups, 1, self.verify)
        self.assertEqual(result["deleted"], [first])
        self.assertEqual(result["retained"], [second])
        self.assertNotIn("candidates", result)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(manual_root.exists())
        self.assertEqual(result["skipped"][0]["name"], manual)

    def test_corrupt_and_old_schema_bundles_are_preserved_without_raw_errors(self):
        corrupt, corrupt_root, _ = self.bundle(1)
        old, old_root, _ = self.bundle(2, revision="previous")
        kept, _, _ = self.bundle(3)
        (corrupt_root / "database.dump").write_bytes(b"SENSITIVE_SENTINEL")
        result = state.prune_backups(self.store, self.backups, 1, self.verify)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["retained"], [kept])
        self.assertEqual({item["name"] for item in result["skipped"]}, {corrupt, old})
        self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))
        self.assertTrue(corrupt_root.exists())
        self.assertTrue(old_root.exists())

    def test_damaged_newest_bundle_never_displaces_last_healthy_backup(self):
        healthy, healthy_root, _ = self.bundle(1)
        damaged, damaged_root, _ = self.bundle(2)
        (damaged_root / "database.dump").write_bytes(b"corrupt")
        self.assertEqual(state.retention_plan(self.store, self.backups, 1)["candidates"], [healthy])
        result = state.prune_backups(self.store, self.backups, 1, self.verify)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["retained"], [healthy])
        self.assertIn(damaged, {item["name"] for item in result["skipped"]})
        self.assertTrue(healthy_root.exists())

    def test_changed_manifest_receipt_hash_and_extra_file_prevent_deletion(self):
        changed, changed_root, manifest = self.bundle(1)
        extra, extra_root, _ = self.bundle(2)
        self.bundle(3)
        (changed_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (extra_root / "unexpected.txt").write_bytes(b"SENSITIVE_SENTINEL")
        result = state.prune_backups(self.store, self.backups, 1, self.verify)
        self.assertEqual(result["deleted"], [])
        self.assertEqual({item["name"] for item in result["skipped"]}, {changed, extra})
        self.assertEqual((extra_root / "unexpected.txt").read_bytes(), b"SENSITIVE_SENTINEL")

    def test_hardlink_bundle_is_not_deleted_and_target_is_untouched(self):
        name, root, _ = self.bundle(1)
        self.bundle(2)
        outside = self.base / "outside.dump"
        os.link(root / "database.dump", outside)
        result = state.prune_backups(self.store, self.backups, 1, self.verify)
        self.assertEqual(result["deleted"], [])
        self.assertIn(name, {item["name"] for item in result["skipped"]})
        self.assertTrue(root.exists())
        self.assertEqual(outside.read_bytes(), b"fixture dump")

    def test_verifier_manifest_mismatch_or_mutation_preserves_whole_bundle(self):
        name, root, _ = self.bundle(1)
        self.bundle(2)

        def changed(path):
            manifest = self.verify(path)
            replacement = path / "replacement"
            replacement.write_bytes((path / "database.dump").read_bytes())
            os.replace(replacement, path / "database.dump")
            return manifest

        for verify in (lambda path: {**self.verify(path), "schema_revision": "changed"}, changed):
            result = state.prune_backups(self.store, self.backups, 1, verify)
            self.assertEqual(result["deleted"], [])
            self.assertIn(name, {item["name"] for item in result["skipped"]})
            self.assertTrue((root / "manifest.json").exists())
            self.assertTrue((root / "database.dump").exists())

    def test_extra_file_added_during_verification_prevents_any_deletion(self):
        old, root, _ = self.bundle(1)
        self.bundle(2)

        def verify(path):
            manifest = self.verify(path)
            if path == root:
                (path / "SENSITIVE_SENTINEL").write_bytes(b"private")
            return manifest

        result = state.prune_backups(self.store, self.backups, 1, verify)
        self.assertEqual(result["deleted"], [])
        self.assertIn(old, {item["name"] for item in result["skipped"]})
        self.assertTrue((root / "database.dump").exists())
        self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
