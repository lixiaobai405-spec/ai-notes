"""No Docker daemon, database, credentials or cloud services are used here."""

from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
schedule = importlib.import_module("site_backup_schedule")


class FakeHost:
    def __init__(self, cfg):
        self.cfg = cfg
        self.commands = []
        self.payloads = []
        self.running = {"db": True, "api": True, "worker": True, "web": True}
        self.ids = {name: str(number) * 64 for name, number in
                    (("db", 1), ("api", 2), ("worker", 3), ("web", 4))}
        self.fail = None
        self.backup_created = None

    def __call__(self, command, **kwargs):
        args = [str(value) for value in command]
        self.commands.append(args)
        if kwargs.get("payload") is not None:
            self.payloads.append(kwargs["payload"])
            if self.fail == "notify":
                raise RuntimeError("secret-key-sentinel")
            return b""
        if "compose" in args:
            return "\n".join(self.ids.values()).encode()
        if "inspect" in args:
            identifier = args[-1]
            name = next(name for name, value in self.ids.items() if value == identifier)
            return json.dumps({"id": identifier, "service": name, "project": "test-site",
                               "running": self.running[name],
                               "health": "healthy" if self.running[name] else None}).encode()
        if "stop" in args:
            if self.fail == "partial_stop":
                self.running["worker"] = False
                raise schedule.ScheduleError("schedule_process_failed")
            for name in ("api", "worker"):
                if self.ids[name] in args:
                    self.running[name] = False
            return b""
        if "start" in args:
            if self.fail == "start":
                raise schedule.ScheduleError("schedule_process_failed")
            name = next(name for name, identifier in self.ids.items() if identifier == args[-1])
            self.running[name] = True
            return b""
        if "backup" in args:
            if self.fail == "backup":
                raise RuntimeError("secret-key-sentinel")
            destination = Path(args[args.index("--output") + 1])
            destination.mkdir()
            (destination / "objects").mkdir()
            content = b"synthetic dump; not a PostgreSQL recovery proof"
            (destination / "database.dump").write_bytes(content)
            created = datetime.strptime(destination.name.split("-")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            manifest = {"schema_revision": "test", "created_at": created.isoformat(),
                        "objects": [], "dump": {"path": "database.dump", "size": len(content),
                                                  "sha256": hashlib.sha256(content).hexdigest()}}
            (destination / "manifest.json").write_text(json.dumps(manifest))
            self.backup_created = destination
            return b'{"ok":true}'
        if "verify" in args:
            if self.fail == "verify":
                raise schedule.ScheduleError("schedule_process_failed")
            return b'{"ok":true}'
        raise AssertionError(args)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        repository = self.root / "repository"
        (repository / "scripts").mkdir(parents=True)
        (repository / "scripts/site_backup.py").write_text("")
        for name in ("compose.yaml", "compose.env", "database-url.txt", "docker", "notify"):
            (self.root / name).write_text("fixture")
        data = {"repository": str(repository), "python": str(Path(sys.executable).resolve()),
                "docker": str(self.root / "docker"), "compose_files": [str(self.root / "compose.yaml")],
                "compose_env_file": str(self.root / "compose.env"), "project_name": "test-site",
                "database_url_file": str(self.root / "database-url.txt"), "storage_backend": "local",
                "storage_root": str(self.root / "objects"), "oss_env_file": None,
                "backup_root": str(self.root / "backups"), "state_root": str(self.root / "state"),
                "writers_exclusive_confirmed": True}
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(data))
        self.cfg = schedule.load_config(self.path)
        self.host = FakeHost(self.cfg)
        self.runner = schedule.Runner(self.cfg, executor=self.host,
                                      clock=lambda: datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc),
                                      sleep=lambda _: None)

    def successful(self):
        with patch.object(schedule, "register_backup") as registered, \
             patch.object(schedule, "prune_backups", return_value={"deleted": [], "retained": [], "skipped": []}) as prune:
            status = self.runner.run()
        return status, registered, prune

    def test_success_orders_backup_before_resume_before_retention(self):
        def prune(*_):
            self.assertTrue(self.host.running["api"] and self.host.running["worker"])
            return {"deleted": []}
        with patch.object(schedule, "register_backup"), patch.object(schedule, "prune_backups", side_effect=prune):
            result = self.runner.run()
        self.assertTrue(result["ok"] and result["backup_verified"] and result["services_restored"])
        commands = self.host.commands
        stopped = next(i for i, cmd in enumerate(commands) if "stop" in cmd)
        backup = next(i for i, cmd in enumerate(commands) if "backup" in cmd)
        verify = next(i for i, cmd in enumerate(commands) if "verify" in cmd)
        start = next(i for i, cmd in enumerate(commands) if "start" in cmd)
        self.assertLess(stopped, backup)
        self.assertLess(backup, verify)
        self.assertLess(verify, start)
        self.assertIsNone(self.runner.store.read("recovery.json"))

    def test_originally_stopped_worker_is_not_started(self):
        self.host.running["worker"] = False
        self.successful()
        self.assertFalse(self.host.running["worker"])
        self.assertFalse(any("start" in cmd and self.host.ids["worker"] in cmd for cmd in self.host.commands))

    def test_failed_backup_restores_services_and_never_prunes(self):
        self.host.fail = "backup"
        with patch.object(schedule, "prune_backups") as prune:
            with self.assertRaisesRegex(schedule.ScheduleError, "^schedule_backup_failed$"):
                self.runner.run()
        prune.assert_not_called()
        self.assertTrue(self.host.running["api"] and self.host.running["worker"])
        self.assertNotIn("secret-key-sentinel", (self.root / "state/status.json").read_text())
        events = list((self.root / "state/events").glob("*.json"))
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].read_text())["notification_status"], "not_configured")

    def test_partial_stop_failure_restores_both(self):
        self.host.fail = "partial_stop"
        with self.assertRaises(schedule.ScheduleError):
            self.runner.run()
        self.assertTrue(self.host.running["worker"] and self.host.running["api"])
        self.assertFalse(any("backup" in cmd for cmd in self.host.commands))

    def test_verify_failure_restores_and_does_not_register(self):
        self.host.fail = "verify"
        with patch.object(schedule, "register_backup") as registered:
            with self.assertRaises(schedule.ScheduleError):
                self.runner.run()
        registered.assert_not_called()
        self.assertTrue(self.host.running["api"])

    def test_restore_failure_preserves_recovery_and_prevents_prune(self):
        self.host.fail = "start"
        with patch.object(schedule, "prune_backups") as prune:
            with self.assertRaisesRegex(schedule.ScheduleError, "schedule_service_restore_failed"):
                self.runner.run()
        self.assertIsNotNone(self.runner.store.read("recovery.json"))
        prune.assert_not_called()
        self.host.fail = None
        self.runner.recover()
        self.assertIsNone(self.runner.store.read("recovery.json"))
        self.assertEqual(self.runner.recover(), {"restored": []})

    def test_incomplete_recovery_is_not_overwritten_by_next_run(self):
        record = {"run_id": "20261025T190000Z-111111111111", "services": []}
        self.runner.store.write("recovery.json", record)
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_recovery_required"):
            self.runner.run()
        self.assertEqual(self.runner.store.read("recovery.json"), record)
        self.assertEqual(self.host.commands, [])

    def test_daytime_run_never_touches_docker(self):
        self.runner.clock = lambda: datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_outside_window"):
            self.runner.run()
        self.assertEqual(self.host.commands, [])

    def test_container_changed_during_maintenance_is_not_started(self):
        self.runner.store.write("recovery.json", {"run_id": "20261025T190000Z-111111111111",
                                                  "services": [{"name": "api", "id": "f" * 64}]})
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_container_identity_changed"):
            self.runner.recover()
        self.assertFalse(any("start" in cmd for cmd in self.host.commands))

    def test_running_migration_rejected_before_stop(self):
        self.host.ids["migrate"] = "5" * 64
        self.host.running["migrate"] = True
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_migration_running"):
            self.runner.run()
        self.assertFalse(any("stop" in cmd for cmd in self.host.commands))

    def test_optional_notification_gets_only_safe_payload(self):
        from dataclasses import replace
        self.runner.cfg = replace(self.cfg, notification_command=(str(self.root / "notify"),))
        self.host.fail = "backup"
        with self.assertRaises(schedule.ScheduleError):
            self.runner.run()
        self.assertEqual(len(self.host.payloads), 1)
        self.assertEqual(set(self.host.payloads[0]), {"event_id", "run_id", "created_at", "phase", "error_code"})
        self.assertNotIn("secret-key-sentinel", json.dumps(self.host.payloads))
        before = len(self.host.payloads)
        self.runner.retry_notifications()
        self.assertEqual(len(self.host.payloads), before)

    def test_failed_notification_can_be_retried_without_repeating_backup(self):
        from dataclasses import replace
        self.runner.cfg = replace(self.cfg, notification_command=(str(self.root / "notify"),))
        status = {"run_id": "20261025T190000Z-111111111111", "phase": "backup", "error_code": "schedule_backup_failed"}
        self.host.fail = "notify"
        self.runner.failure(status)
        first_id = self.host.payloads[0]["event_id"]
        self.host.fail = None
        self.runner.retry_notifications()
        self.assertEqual(self.host.payloads[-1]["event_id"], first_id)
        self.assertFalse(any("backup" in cmd for cmd in self.host.commands))

    def test_state_write_failure_during_restore_does_not_prevent_start(self):
        original = self.runner.phase
        def phase(status, value):
            if value == "restore":
                raise OSError("full disk secret-key-sentinel")
            return original(status, value)
        with patch.object(self.runner, "phase", side_effect=phase):
            self.successful()
        self.assertTrue(self.host.running["api"] and self.host.running["worker"])

    def test_config_rejects_unsafe_roots_unknown_keys_and_bad_keep_count(self):
        original = json.loads(self.path.read_text())
        for change in ({"state_root": original["backup_root"]}, {"keep_last": 0},
                       {"password": "secret-key-sentinel"}, {"compose_files": ["relative.yaml"]}):
            self.path.write_text(json.dumps({**original, **change}))
            with self.assertRaises(schedule.ScheduleError):
                schedule.load_config(self.path)

    def test_check_has_no_process_or_directory_side_effect(self):
        schedule.check_config(self.cfg)
        self.assertEqual(self.host.commands, [])
        self.assertFalse(self.cfg.backup_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX execution is tested in the isolated Linux container")
    def test_process_failure_and_timeout_never_expose_child_output(self):
        for code, timeout in (("print('secret-key-sentinel');raise SystemExit(1)", 5),
                              ("import time;time.sleep(10)", 0.1)):
            started = time.monotonic()
            with self.assertRaises(schedule.ScheduleError) as error:
                schedule.execute([sys.executable, "-c", code], cwd=self.root, timeout=timeout)
            self.assertNotIn("secret-key-sentinel", str(error.exception))
            self.assertLess(time.monotonic() - started, 6)

    def test_interruption_during_backup_resumes_services(self):
        def host(command, **kwargs):
            if "backup" in command:
                raise schedule.Interrupted()
            return self.host(command, **kwargs)
        self.runner.executor = host
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_interrupted"):
            self.runner.run()
        self.assertTrue(self.host.running["api"] and self.host.running["worker"])

    def test_worker_restore_failure_does_not_prevent_api_recovery(self):
        def host(command, **kwargs):
            if "start" in command and self.host.ids["worker"] in command:
                raise schedule.ScheduleError("schedule_process_failed")
            return self.host(command, **kwargs)
        self.runner.executor = host
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_service_restore_failed"):
            self.runner.run()
        self.assertTrue(self.host.running["api"])
        self.assertFalse(self.host.running["worker"])
        self.assertIsNotNone(self.runner.store.read("recovery.json"))

    def test_notification_state_failure_never_masks_primary_backup_error(self):
        original = self.runner.store.write
        def write(name, value):
            if name.startswith("events/") and value["notification_status"] != "pending":
                raise schedule.ScheduleError("schedule_state_unavailable")
            return original(name, value)
        self.host.fail = "backup"
        with patch.object(self.runner.store, "write", side_effect=write):
            with self.assertRaisesRegex(schedule.ScheduleError, "^schedule_backup_failed$"):
                self.runner.run()
        self.assertTrue(self.host.running["api"])

    def test_large_retention_result_is_persisted_as_bounded_summary(self):
        large = {"deleted": ["scheduled-20261025T190000Z-111111111111"] * 3000,
                 "retained": [], "skipped": []}
        with patch.object(schedule, "register_backup"), patch.object(schedule, "prune_backups", return_value=large):
            status = self.runner.run()
        self.assertEqual(status["retention"]["deleted_count"], 3000)
        self.assertLessEqual(len(status["retention"]["deleted"]), 20)
        self.assertLess((self.root / "state/status.json").stat().st_size, 65536)

    def test_unconfirmed_external_writers_prevents_process_changes(self):
        from dataclasses import replace
        self.runner.cfg = replace(self.cfg, writers_exclusive_confirmed=False)
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_maintenance_not_confirmed"):
            self.runner.run()
        self.assertEqual(self.host.commands, [])

    @unittest.skipUnless(os.name == "posix", "POSIX execution is tested in the isolated Linux container")
    def test_large_process_output_is_bounded(self):
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_process_output_limit"):
            schedule.execute([sys.executable, "-c", "print('x'*100000)"], cwd=self.root, timeout=5)

    def test_systemd_defaults_and_optional_database_binding(self):
        service = (ROOT / "ops/systemd/ai-notes-backup.service").read_text()
        timer = (ROOT / "ops/systemd/ai-notes-backup.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStopPost=", service)
        self.assertIn("TimeoutStartSec=", service)
        self.assertNotIn("RuntimeMaxSec=", service)
        self.assertIn("03:00:00 Asia/Shanghai", timer)
        self.assertIn("Persistent=false", timer)
        override = (ROOT / "compose.backup.yaml").read_text()
        self.assertIn('"127.0.0.1:55435:5432"', override)
        example = json.loads((ROOT / "ops/backup-schedule.example.json").read_text())
        self.assertEqual(example["keep_last"], 7)
        self.assertEqual(example["maintenance_start_utc"], "19:00")
        self.assertFalse(example["writers_exclusive_confirmed"])
        self.assertEqual(example["notification_command"], [])

    def test_two_runs_integrate_real_receipts_and_retention_without_database(self):
        from dataclasses import replace
        self.runner.cfg = replace(self.cfg, keep_last=1)
        self.runner.clock = lambda: datetime(2020, 1, 1, 19, 0, tzinfo=timezone.utc)
        first = self.runner.run()
        original = self.host.backup_created
        self.assertTrue(original.exists())
        self.assertEqual(first["retention"]["retained_count"], 1)
        manual = self.cfg.backup_root / "manual-backup-do-not-delete"
        manual.mkdir()
        self.runner.clock = lambda: datetime(2020, 1, 2, 19, 0, tzinfo=timezone.utc)
        second = self.runner.run()
        self.assertEqual(second["retention"]["deleted_count"], 1)
        self.assertFalse(original.exists())
        self.assertTrue(self.host.backup_created.exists() and manual.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX execution is tested in the isolated Linux container")
    def test_notification_process_reads_stdin_with_no_output_or_network(self):
        command = [sys.executable, "-c",
                   "import json,sys; p=json.load(sys.stdin); assert p['event_id']=='fixture'; print('secret-key-sentinel')"]
        result = schedule.execute(command, cwd=self.root, timeout=5, payload={"event_id": "fixture"})
        self.assertEqual(result, b"")

    @unittest.skipUnless(os.name == "posix", "POSIX execution is tested in the isolated Linux container")
    def test_exited_parent_with_live_stdout_descendant_is_still_bounded(self):
        started = time.monotonic()
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)'])"
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_process_timeout"):
            schedule.execute([sys.executable, "-c", code], cwd=self.root, timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)

    def test_changed_worker_identity_does_not_block_unchanged_api_recovery(self):
        self.host.running["api"] = self.host.running["worker"] = False
        self.runner.store.write("recovery.json", {"run_id": "20261025T190000Z-111111111111", "services": [
            {"name": "worker", "id": "f" * 64}, {"name": "api", "id": self.host.ids["api"]}]})
        with self.assertRaisesRegex(schedule.ScheduleError, "schedule_container_identity_changed"):
            self.runner.recover()
        self.assertTrue(self.host.running["api"])
        self.assertFalse(self.host.running["worker"])

    def test_nonposix_execution_refuses_to_spawn_process(self):
        import site_backup_process
        with patch.object(site_backup_process.os, "name", "nt"), \
             patch.object(site_backup_process.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(schedule.ScheduleError, "schedule_platform_unsupported"):
                schedule.execute([sys.executable, "-c", "pass"], cwd=self.root, timeout=1)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
