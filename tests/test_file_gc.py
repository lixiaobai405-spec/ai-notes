"""Whole collector lifecycle on temporary objects, without a database or cloud."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.file_gc import FileCollector, digest, known_key, main
from scripts.file_gc_common import FileGCError
from scripts.file_gc_state import PrivateState
from scripts.file_gc_support import LocalGCStorage

NOW = datetime(2026, 10, 25, 14, tzinfo=timezone.utc)


class References:
    def __init__(self):
        self.keys, self.active, self.guards = set(), 0, []

    @contextmanager
    def snapshot(self, writing=False):
        self.guards.append(writing)
        yield {"keys": set(self.keys), "active_writers": self.active,
               "counts": {}, "fingerprint": digest({"keys": sorted(self.keys), "active": self.active})}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.objects = self.base / "live"; self.objects.mkdir(mode=0o700)
        self.state = PrivateState(self.base / "private")
        self.storage = LocalGCStorage(self.objects)
        self.refs = References()
        self.gc = FileCollector(self.storage, self.state, self.refs, now=lambda: NOW)

    def put(self, body=b"orphan", *, age=8, key=None):
        key = key or f"{uuid4()}/{uuid4()}/{uuid4()}-reading.jpg"
        target = self.objects.joinpath(*key.split("/")); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        modified = (NOW - timedelta(days=age)).timestamp(); os.utime(target, (modified, modified))
        return key, target

    def apply_plan(self, plan):
        return self.gc.apply(plan["plan"], plan["digest"], maintenance_confirmed=True, writers_stopped=True)

    def restore(self, result):
        return self.gc.restore(result["bundle"], result["digest"], maintenance_confirmed=True, writers_stopped=True)

    def test_plan_retains_references_unknown_recent_and_boundary_without_deleting(self):
        retained, kept = self.put(); self.refs.keys.add(retained)
        _, old = self.put(); _, recent = self.put(age=6); _, boundary = self.put(age=7)
        _, unknown = self.put(key="manual/old-object.jpg")
        plan = self.gc.plan()
        self.assertEqual({k: plan[k] for k in ("total", "referenced", "unknown", "young", "candidates")},
                         {"total": 5, "referenced": 1, "unknown": 1, "young": 2, "candidates": 1})
        self.assertTrue(all(p.exists() for p in (kept, old, recent, boundary, unknown)))
        self.assertEqual(self.refs.guards, [False])

    def test_native_and_restore_key_allowlist_never_accepts_traversal_or_near_matches(self):
        owner, file, token = str(uuid4()), str(uuid4()), str(uuid4())
        for key in (f"{owner}/{file}/original", f"{owner}/{file}/{token}-thumbnail.jpg",
                    f"restore_{uuid4().hex}/{uuid4().hex}/original",
                    f"restore_{uuid4().hex}/{uuid4().hex}/v_{uuid4().hex}"):
            self.assertTrue(known_key(key), key)
        for key in ("owner/file/original", f"{owner}/{file}/../original", f"{owner}/{file}/{token}-other.jpg",
                    f"{owner}/{file}/original/extra", f"{owner.upper()}/{file}/original", "", None):
            self.assertFalse(known_key(key), key)

    def test_all_payloads_are_durable_before_first_delete_and_restore_is_idempotent(self):
        _, first = self.put(b"first"); _, second = self.put(b"second")
        plan = self.gc.plan(); original_delete = self.storage.delete; checked = []
        def deletion(item):
            bundles = list(self.state.root.glob("run-*")); self.assertEqual(len(bundles), 1)
            manifest = PrivateState(bundles[0]).read("manifest.json")
            self.assertEqual(len(manifest["objects"]), 2)
            for obj in manifest["objects"]:
                self.assertEqual(hashlib.sha256((bundles[0] / obj["payload"]).read_bytes()).hexdigest(), obj["sha256"])
            checked.append(item.key)
            return original_delete(item)
        with patch.object(self.storage, "delete", side_effect=deletion):
            result = self.apply_plan(plan)
        self.assertEqual(result["status"], "complete"); self.assertEqual(len(checked), 2)
        self.assertFalse(first.exists() or second.exists()); self.assertEqual(self.refs.guards[-1], True)
        restored = self.restore(result)
        self.assertEqual(restored["status"], "restored"); self.assertEqual(restored["restored"], 2)
        self.assertEqual(first.read_bytes(), b"first"); self.assertEqual(second.read_bytes(), b"second")
        self.assertEqual(self.restore(result)["restored"], 2)

    def test_empty_plan_never_creates_bundle(self):
        plan = self.gc.plan(); self.assertEqual(self.apply_plan(plan), {"status": "nothing_to_delete", "deleted": 0})
        self.assertEqual(list(self.state.root.glob("run-*")), [])

    def test_both_explicit_maintenance_attestations_are_required(self):
        self.put(); plan = self.gc.plan()
        for kwargs in ({}, {"maintenance_confirmed": True}, {"writers_stopped": True}):
            with self.assertRaisesRegex(FileGCError, "gc_maintenance_required"):
                self.gc.apply(plan["plan"], plan["digest"], **kwargs)
        self.assertEqual(list(self.state.root.glob("run-*")), [])

    def test_plan_cannot_be_reused_after_delete_or_with_changed_references(self):
        key, target = self.put(); plan = self.gc.plan(); self.refs.keys.add(key)
        with self.assertRaisesRegex(FileGCError, "gc_references_changed"):
            self.apply_plan(plan)
        self.assertTrue(target.exists()); self.assertEqual(list(self.state.root.glob("run-*")), [])

    def test_active_writers_block_even_with_matching_plan(self):
        _, target = self.put(); self.refs.active = 1; plan = self.gc.plan()
        with self.assertRaisesRegex(FileGCError, "gc_writers_active"):
            self.apply_plan(plan)
        self.assertTrue(target.exists())

    def test_object_drift_and_backend_change_stop_before_bundle(self):
        _, target = self.put(); plan = self.gc.plan(); target.write_bytes(b"changed")
        with self.assertRaisesRegex(FileGCError, "gc_object_changed"):
            self.apply_plan(plan)
        self.assertEqual(list(self.state.root.glob("run-*")), [])
        with patch.object(self.storage, "backend_identity", return_value={"backend": "local", "root": str(self.base / "other")}):
            with self.assertRaisesRegex(FileGCError, "gc_backend_changed"):
                self.apply_plan(plan)

    def test_forged_digest_and_recomputed_unsafe_plan_are_rejected(self):
        self.put(); plan = self.gc.plan()
        with self.assertRaisesRegex(FileGCError, "gc_plan_changed"):
            self.gc.apply(plan["plan"], "0" * 64, maintenance_confirmed=True, writers_stopped=True)
        doc = self.state.read(plan["plan"]); doc["objects"][0]["key"] = "../../secret"
        doc["digest"] = digest({k: v for k, v in doc.items() if k != "digest"}); self.state.write(plan["plan"], doc)
        with self.assertRaisesRegex(FileGCError, "gc_plan_invalid"):
            self.apply_plan({**plan, "digest": doc["digest"]})

    def test_grace_and_expired_or_future_plan_are_rejected(self):
        for value in (True, 6, 0, 3651, "7"):
            with self.assertRaises(FileGCError): self.gc.plan(grace_days=value)
        self.put(); plan = self.gc.plan()
        for now in (NOW + timedelta(hours=25), NOW - timedelta(seconds=1)):
            self.gc.now = lambda: now
            with self.assertRaisesRegex(FileGCError, "gc_plan_expired"): self.apply_plan(plan)

    def test_export_failure_or_bad_archive_never_deletes(self):
        _, target = self.put(); plan = self.gc.plan()
        with patch.object(self.storage, "export", side_effect=OSError("private provider response")), patch.object(self.storage, "delete") as deletion:
            result = self.apply_plan(plan)
        self.assertEqual(result["status"], "stopped"); self.assertTrue(target.exists()); deletion.assert_not_called()
        self.assertNotIn("private provider", json.dumps(result))

    def test_archive_hash_validation_before_delete(self):
        _, target = self.put(); plan = self.gc.plan(); export = self.storage.export
        def corrupted(item, path):
            captured = export(item, path); path.write_bytes(b"corrupted"); return captured
        with patch.object(self.storage, "export", side_effect=corrupted), patch.object(self.storage, "delete") as deletion:
            result = self.apply_plan(plan)
        self.assertEqual(result["status"], "stopped"); self.assertTrue(target.exists()); deletion.assert_not_called()

    def test_delete_response_lost_stops_following_deletes_and_local_bytes_restore(self):
        self.put(b"A"); self.put(b"B"); plan = self.gc.plan(); deletion = self.storage.delete; calls = []
        def unknown(item):
            calls.append(item.key); deletion(item); raise RuntimeError("credential raw response")
        with patch.object(self.storage, "delete", side_effect=unknown): result = self.apply_plan(plan)
        self.assertEqual(result["status"], "stopped"); self.assertEqual(result["unknown"], 1); self.assertEqual(len(calls), 1)
        self.assertEqual(self.restore(result)["status"], "restored")
        self.assertEqual(sorted(path.read_bytes() for path in self.objects.rglob("*.jpg")), [b"A", b"B"])

    def test_delete_intent_write_failure_preserves_live_object(self):
        _, target = self.put(); plan = self.gc.plan(); write = PrivateState.write
        def fail_before_delete(state, name, document):
            if any(item.get("status") == "deleting" for item in document.get("objects", [])):
                raise FileGCError("gc_state_unavailable")
            return write(state, name, document)
        with patch.object(PrivateState, "write", fail_before_delete), patch.object(self.storage, "delete") as deletion:
            with self.assertRaisesRegex(FileGCError, "gc_state_unavailable"): self.apply_plan(plan)
        deletion.assert_not_called(); self.assertTrue(target.exists())

    def test_restore_uses_verified_bundle_and_never_overwrites_new_content(self):
        _, target = self.put(); result = self.apply_plan(self.gc.plan()); target.write_bytes(b"new content")
        restored = self.restore(result)
        self.assertEqual(restored["status"], "restore_stopped"); self.assertEqual(target.read_bytes(), b"new content")
        self.assertEqual(restored["error_code"], "gc_restore_conflict")

    def test_restore_rejects_wrong_digest_missing_payload_and_active_writers(self):
        self.put(); result = self.apply_plan(self.gc.plan())
        with self.assertRaisesRegex(FileGCError, "gc_bundle_changed"):
            self.restore({**result, "digest": "0" * 64})
        self.refs.active = 1
        with self.assertRaisesRegex(FileGCError, "gc_writers_active"): self.restore(result)
        self.refs.active = 0
        bundle = self.state.bundle(result["bundle"]); next((bundle / "objects").iterdir()).unlink()
        with self.assertRaisesRegex(FileGCError, "gc_bundle_invalid"): self.restore(result)

    def test_storage_and_private_state_must_be_separate(self):
        for state in (PrivateState(self.objects / "nested"), PrivateState(self.base)):
            with self.assertRaisesRegex(FileGCError, "gc_paths_overlap"):
                FileCollector(self.storage, state, self.refs)

    def test_cli_errors_never_print_provider_exception_or_configuration(self):
        import io
        output = io.StringIO()
        with patch("scripts.file_gc.configured", side_effect=RuntimeError("secret database URL")), patch("sys.stdout", output):
            self.assertEqual(main(["--config", "private.json", "plan"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "code": "gc_operation_failed"})

    def test_collector_deadline_interrupts_the_actual_inventory_loop(self):
        self.put(); self.put(); self.gc.timeout = 1
        # Initial start, first inventory checkpoint, then next directory entry.
        with patch("scripts.file_gc.time.monotonic", side_effect=[0, .1, .2, 2]):
            with self.assertRaisesRegex(FileGCError, "gc_timeout"):
                self.gc.plan()
        self.assertEqual(list(self.state.root.glob("plan-*")), [])

    def test_local_case_and_trailing_dot_alias_references_are_retained(self):
        key, target = self.put()
        for alias in (key.upper(), "/".join(part + "." for part in key.split("/")),
                      "/".join(part.upper() + ". " for part in key.split("/"))):
            self.refs.keys = {alias}
            plan = self.gc.plan()
            self.assertEqual(plan["referenced"], 1)
            self.assertEqual(plan["candidates"], 0)
            self.assertEqual(self.apply_plan(plan)["status"], "nothing_to_delete")
            self.assertTrue(target.exists())

    def test_apply_rechecks_local_alias_reference_even_in_a_forged_old_plan(self):
        key, target = self.put(); self.refs.keys = {key.upper()}
        # Simulate a plan from an older tool that performed exact string matching.
        plan = self.gc.plan(); doc = self.state.read(plan["plan"])
        doc["objects"] = [self.storage.head(key).document()]
        doc["digest"] = digest({k: v for k, v in doc.items() if k != "digest"})
        self.state.write(plan["plan"], doc)
        with self.assertRaisesRegex(FileGCError, "gc_references_changed"):
            self.apply_plan({**plan, "digest": doc["digest"]})
        self.assertTrue(target.exists())

    def test_versioned_oss_collector_archives_creates_marker_and_restores_only_own_marker(self):
        from types import SimpleNamespace
        from tests.test_file_gc_support import FakeOSS, DATA, HAS_OSS
        if not HAS_OSS:
            self.skipTest("locked OSS SDK not installed")
        from scripts.file_gc_support import OSSGCStorage
        client = FakeOSS(); key = f"{uuid4()}/{uuid4()}/{uuid4()}-reading.jpg"
        client.objects["private/" + key] = [client.data_version("data-version")]
        storage = OSSGCStorage(SimpleNamespace(oss_bucket="fixture-bucket", oss_prefix="private/", oss_region="cn-hangzhou"), client=client)
        collector = FileCollector(storage, self.state, self.refs, now=lambda: NOW)
        plan = collector.plan(); self.assertEqual(plan["candidates"], 1)
        applied = collector.apply(plan["plan"], plan["digest"], maintenance_confirmed=True, writers_stopped=True)
        self.assertEqual(applied["status"], "complete")
        self.assertTrue(client.objects["private/" + key][0]["marker"])
        self.assertEqual(client.objects["private/" + key][1]["data"], DATA)
        restored = collector.restore(applied["bundle"], applied["digest"], maintenance_confirmed=True, writers_stopped=True)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(client.objects["private/" + key][0]["version_id"], "data-version")
        deletions = [call[1].version_id for call in client.calls if call[0] == "delete"]
        self.assertEqual(deletions, [None, "marker-1"])


if __name__ == "__main__":
    unittest.main()
