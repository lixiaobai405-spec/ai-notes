"""Private monitoring orchestration: no Docker, database, provider or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.file_gc_state import PrivateState
from scripts.monitoring_common import MonitoringError
from scripts.site_monitor import SiteMonitor, STATE_NAME, main, validate_config

NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def host_fixture(disks, project, services, **kwargs):
    return {"status": "ok", "memory": {"status": "ok", "total_bytes": 100, "available_bytes": 80, "used_percent": 20},
        "disks": {alias: {"status": "ok", "total_bytes": 100, "free_bytes": 80, "used_percent": 20} for alias in disks},
        "containers": {"status": "ok", "services": {name: {"status": "ok", "running_count": 1, "container_count": 1,
            "cpu_percent": 1, "memory_usage_bytes": 10, "memory_limit_bytes": 100, "memory_used_percent": 10} for name in services}}}


def database_fixture(url, **kwargs):
    return {"status": "ok", "connections": {"status": "ok", "total": 2, "max_connections": 100, "used_percent": 2},
        "jobs": {"status": "ok", "ready_queued": 0, "scheduled_queued": 0, "running": 0, "failed_total": 0,
            "failed_created_window": 0, "oldest_ready_seconds": 0, "expired_leases": 0},
        "ai": {"status": "ok", "queued": 0, "running": 0, "errors_finished_window": 0,
            "provider_errors_finished_window": 0, "oldest_queued_seconds": 0, "expired_leases": 0},
        "uploads": {"status": "ok", "created_window": 0, "expired_created_window": 0, "rejected_created_window": 0,
            "processing_failed_created_window": 0, "pdf_rejected_created_window": 0,
            "http_failure_rate": None, "http_failure_rate_status": "unavailable"},
        "usage": {"status": "ok", "known_total_tokens": 0, "terminal_runs_window": 0, "missing_usage_runs": 0,
            "incomplete_usage_runs": 0, "cost": None, "cost_status": "unavailable"}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cfg = json.loads((ROOT / "ops/monitoring.example.json").read_text())
        self.cfg.update(state_root=str(self.base / "state"), disks={name: str(self.base) for name in self.cfg["disks"]})
        self.clock = NOW
        self.monitor = SiteMonitor(self.cfg, host_collector=host_fixture, database_collector=database_fixture, now=lambda: self.clock)
        self.database_url = "postgresql+psycopg://synthetic-user:synthetic-password@synthetic-db.invalid:5432/notes"
        self.env = patch.dict(os.environ, {self.cfg["database_url_env"]: self.database_url})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_healthy_sample_and_status_have_no_secret_or_resource_names(self):
        result = self.monitor.sample()
        self.assertEqual(result["status"], "ok"); self.assertEqual(self.monitor.status()["status"], "ok")
        encoded = json.dumps(result)
        self.assertNotIn(self.database_url, encoded); self.assertNotIn(str(self.base), encoded)
        saved = self.monitor.state.read(STATE_NAME)
        for private in ("synthetic-user", "synthetic-password", "synthetic-db.invalid", self.database_url):
            self.assertNotIn(private, json.dumps(saved))
        self.assertEqual(saved["snapshot"]["metrics"]["database"]["usage"]["cost_status"], "unavailable")
        self.assertEqual(saved["attempt"]["status"], "complete")

    def test_partial_failure_does_not_keep_previous_healthy_state(self):
        self.monitor.sample(); self.clock += timedelta(seconds=60)
        self.monitor.database_collector = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret-password-and-url"))
        result = self.monitor.sample(); self.assertEqual(result["status"], "unknown")
        saved = self.monitor.state.read(STATE_NAME)
        self.assertEqual(saved["snapshot"]["metrics"]["database"]["status"], "unavailable")
        self.assertNotIn("secret-password", json.dumps(saved))

    def test_missing_database_environment_is_unknown_not_zero(self):
        with patch.dict(os.environ, {}, clear=True): result = self.monitor.sample()
        self.assertEqual(result["status"], "unknown")
        section = self.monitor.state.read(STATE_NAME)["snapshot"]["metrics"]["database"]
        self.assertEqual(section, {"status": "unavailable", "code": "monitoring_database_environment_missing"})

    def test_critical_alert_deduplicates_and_recovers_when_observed_healthy(self):
        def full(*args, **kwargs):
            value = host_fixture(*args, **kwargs); value["memory"]["used_percent"] = 99; return value
        self.monitor.host_collector = full
        self.assertEqual(self.monitor.sample()["status"], "critical")
        initial = self.monitor.state.read(STATE_NAME)
        self.clock += timedelta(seconds=60); self.monitor.sample()
        repeated = self.monitor.state.read(STATE_NAME)
        self.assertEqual(len(initial["history"]), len(repeated["history"]))
        self.monitor.host_collector = host_fixture; self.clock += timedelta(seconds=60)
        self.assertEqual(self.monitor.sample()["status"], "ok")
        final = self.monitor.state.read(STATE_NAME)
        self.assertFalse(final["active"]); self.assertEqual(final["history"][-1]["state"], "resolved")

    def test_stale_future_and_incomplete_snapshots_never_show_ok(self):
        self.assertEqual(self.monitor.status()["code"], "monitoring_snapshot_missing")
        self.monitor.sample(); self.clock += timedelta(seconds=181)
        self.assertEqual(self.monitor.status()["code"], "monitoring_snapshot_stale")
        self.clock = NOW - timedelta(seconds=6)
        self.assertEqual(self.monitor.status()["code"], "monitoring_clock_changed")
        value = self.monitor.state.read(STATE_NAME); value["attempt"]["status"] = "running"
        self.monitor.state.write(STATE_NAME, value)
        self.assertEqual(self.monitor.status()["code"], "monitoring_sample_incomplete")

    def test_status_is_state_only_and_policy_changes_invalidate_old_evaluation(self):
        self.monitor.sample(); changed = deepcopy(self.cfg); changed["thresholds"][0]["warning"] = 10
        reader = SiteMonitor(changed, host_collector=lambda *a, **k: self.fail("not read only"), database_collector=lambda *a, **k: self.fail("not read only"), now=lambda: NOW)
        self.assertEqual(reader.status()["code"], "monitoring_policy_changed")

    def test_target_changes_cannot_reuse_history_or_overwrite_snapshot(self):
        self.monitor.sample(); raw = (self.monitor.state.root / STATE_NAME).read_bytes()
        changed = deepcopy(self.cfg); changed["compose_project"] = "other-project"
        reader = SiteMonitor(changed, now=lambda: NOW)
        self.assertEqual(reader.status()["code"], "monitoring_target_changed")
        with self.assertRaisesRegex(MonitoringError, "monitoring_target_changed"): reader.sample()
        self.assertEqual((self.monitor.state.root / STATE_NAME).read_bytes(), raw)

    def test_constructor_does_not_read_database_environment(self):
        with patch.object(os.environ, "get", side_effect=AssertionError("no environment read in constructor")):
            SiteMonitor(self.cfg, state=self.monitor.state)

    def test_database_target_changes_in_same_environment_refuse_to_reuse_state(self):
        self.monitor.sample()
        raw = (self.monitor.state.root / STATE_NAME).read_bytes()
        alternatives = (
            self.database_url.replace("synthetic-db.invalid", "other-db.invalid"),
            self.database_url.replace(":5432/", ":5433/"),
            self.database_url.replace("/notes", "/other_notes"),
            self.database_url + "?host=other-db.invalid",
            self.database_url + "?hostaddr=192.0.2.2",
            self.database_url + "?service=other-service",
        )
        for alternative in alternatives:
            with self.subTest(target=alternative.rsplit("@", 1)[-1]):
                with patch.dict(os.environ, {self.cfg["database_url_env"]: alternative}):
                    self.assertEqual(self.monitor.status()["code"], "monitoring_target_changed")
                    with self.assertRaisesRegex(MonitoringError, "monitoring_target_changed"):
                        self.monitor.sample()
                self.assertEqual((self.monitor.state.root / STATE_NAME).read_bytes(), raw)

    def test_credential_rotation_and_equivalent_url_do_not_change_database_target(self):
        self.monitor.sample()
        first_target = self.monitor.state.read(STATE_NAME)["database_target"]
        rotated = "postgresql://rotation-user:rotation-password@SYNTHETIC-DB.INVALID/notes"
        with patch.dict(os.environ, {self.cfg["database_url_env"]: rotated}):
            self.assertEqual(self.monitor.status()["status"], "ok")
            self.assertEqual(self.monitor.sample()["status"], "ok")
        self.assertEqual(self.monitor.state.read(STATE_NAME)["database_target"], first_target)

    def test_nonrouting_query_options_do_not_change_database_target(self):
        self.monitor.sample()
        changed = self.database_url + "?application_name=rotated-client&connect_timeout=3&sslmode=require"
        with patch.dict(os.environ, {self.cfg["database_url_env"]: changed}):
            self.assertEqual(self.monitor.status()["status"], "ok")
            self.assertEqual(self.monitor.sample()["status"], "ok")

    def test_libpq_routing_environment_changes_refuse_to_reuse_state(self):
        routing = {"PGHOST": "synthetic-db.invalid", "PGHOSTADDR": "192.0.2.1", "PGPORT": "5432",
                   "PGSERVICE": "synthetic-service", "PGSERVICEFILE": "/synthetic/service.conf",
                   "PGSYSCONFDIR": "/synthetic/config", "PGOPTIONS": "-c search_path=public",
                   "PGTARGETSESSIONATTRS": "read-write", "PGLOADBALANCEHOSTS": "disable"}
        with patch.dict(os.environ, {self.cfg["database_url_env"]: "postgresql+psycopg:///notes", **routing}):
            self.monitor.sample()
            raw = (self.monitor.state.root / STATE_NAME).read_bytes()
            for key in routing:
                with self.subTest(variable=key), patch.dict(os.environ, {key: "changed-routing-value"}):
                    self.assertEqual(self.monitor.status()["code"], "monitoring_target_changed")
                    with self.assertRaisesRegex(MonitoringError, "monitoring_target_changed"):
                        self.monitor.sample()
                    self.assertEqual((self.monitor.state.root / STATE_NAME).read_bytes(), raw)
            saved = json.dumps(self.monitor.state.read(STATE_NAME))
        for key, value in routing.items():
            self.assertNotIn(key, saved)
            if not value.isdecimal():
                self.assertNotIn(value, saved)

    def test_libpq_credential_environment_rotation_does_not_change_database_target(self):
        with patch.dict(os.environ, {self.cfg["database_url_env"]: "postgresql+psycopg:///notes",
                                    "PGUSER": "synthetic-user", "PGPASSWORD": "synthetic-password"}):
            self.monitor.sample()
            first_target = self.monitor.state.read(STATE_NAME)["database_target"]
            with patch.dict(os.environ, {"PGUSER": "rotated-user", "PGPASSWORD": "rotated-password"}):
                self.assertEqual(self.monitor.status()["status"], "ok")
                self.assertEqual(self.monitor.sample()["status"], "ok")
            saved = self.monitor.state.read(STATE_NAME)
        self.assertEqual(saved["database_target"], first_target)
        for secret in ("synthetic-user", "synthetic-password", "rotated-user", "rotated-password"):
            self.assertNotIn(secret, json.dumps(saved))

    def test_missing_environment_keeps_database_critical_and_original_target(self):
        def exhausted(*args, **kwargs):
            value = database_fixture(*args, **kwargs)
            value["connections"]["used_percent"] = 99
            return value
        self.monitor.database_collector = exhausted
        self.assertEqual(self.monitor.sample()["status"], "critical")
        before = self.monitor.state.read(STATE_NAME)
        self.clock += timedelta(seconds=60)
        with patch.dict(os.environ, {}, clear=True):
            status = self.monitor.status()
            self.assertEqual(status["status"], "unknown")
            self.assertEqual(status["code"], "monitoring_database_environment_missing")
            result = self.monitor.sample()
        after = self.monitor.state.read(STATE_NAME)
        key = "database.connections.used_percent"
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(after["database_target"], before["database_target"])
        self.assertEqual(after["active"][key]["id"], before["active"][key]["id"])
        self.assertEqual(after["active"][key]["severity"], "critical")
        self.assertFalse(any(item["key"] == key and item["state"] == "resolved" for item in after["history"]))

    def test_invalid_database_environment_is_unknown_and_never_echoed(self):
        self.monitor.sample()
        with patch.dict(os.environ, {self.cfg["database_url_env"]: "invalid-private-password"}):
            self.assertEqual(self.monitor.status()["code"], "monitoring_database_invalid")
            self.assertEqual(self.monitor.sample()["status"], "unknown")
        self.assertNotIn("invalid-private-password", json.dumps(self.monitor.state.read(STATE_NAME)))

    def test_database_cutoff_matches_snapshot_observation_before_host_collection(self):
        def delayed_host(*args, **kwargs):
            self.clock += timedelta(seconds=8)
            return host_fixture(*args, **kwargs)
        def database(url, **kwargs):
            self.assertEqual(kwargs["now"], NOW)
            return database_fixture(url, **kwargs)
        self.monitor.host_collector, self.monitor.database_collector = delayed_host, database
        self.assertEqual(self.monitor.sample()["status"], "ok")
        saved = self.monitor.state.read(STATE_NAME)
        self.assertEqual(saved["snapshot"]["observed_at"], NOW.isoformat())
        self.assertEqual(saved["attempt"]["finished_at"], (NOW + timedelta(seconds=8)).isoformat())

    def test_sampling_holds_single_instance_lock(self):
        with self.monitor.state.lock():
            with self.assertRaisesRegex(MonitoringError, "monitoring_locked"): self.monitor.sample()

    def test_failed_final_write_leaves_attempt_incomplete(self):
        write = self.monitor.state.write; calls = []
        def fail(name, document):
            calls.append(name)
            if len(calls) == 2: raise OSError("private storage failure")
            return write(name, document)
        with patch.object(self.monitor.state, "write", side_effect=fail):
            with self.assertRaisesRegex(MonitoringError, "monitoring_state_unavailable"): self.monitor.sample()
        self.assertEqual(self.monitor.status()["code"], "monitoring_snapshot_missing")
        self.assertEqual(self.monitor.state.read(STATE_NAME)["attempt"]["status"], "running")

    def test_malformed_state_or_nonfinite_collector_cannot_be_reported_healthy(self):
        self.monitor.host_collector = lambda *a, **k: {"status": "ok", "memory": float("nan")}
        self.assertEqual(self.monitor.sample()["status"], "unknown")
        value = self.monitor.state.read(STATE_NAME); value["snapshot"]["evaluation"]["status"] = "secret-string"
        self.monitor.state.write(STATE_NAME, value)
        with self.assertRaisesRegex(MonitoringError, "monitoring_state_invalid"): self.monitor.status()

    def test_config_is_strict_and_requires_all_disk_thresholds(self):
        for changes in ({"password": "forbidden"}, {"sample_timeout_seconds": True}, {"stale_after_seconds": 10},
                        {"services": ["api", "api"]}, {"disks": {"system": "relative"}}, {"database_url_env": "not-a-variable"}):
            with self.subTest(changes=changes):
                with self.assertRaises(MonitoringError): validate_config({**self.cfg, **changes})
        cfg = deepcopy(self.cfg); cfg["thresholds"] = cfg["thresholds"][1:]
        with self.assertRaisesRegex(MonitoringError, "monitoring_thresholds_missing"): validate_config(cfg)

    def test_check_and_status_never_invoke_collectors(self):
        private = PrivateState(self.base / "config"); private.write("config.json", self.cfg)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch.object(SiteMonitor, "sample", side_effect=AssertionError("no collection")):
            self.assertEqual(main(["check", "--config", str(private.root / "config.json")]), 0)
            self.assertEqual(main(["status", "--config", str(private.root / "config.json")]), 2)
        self.assertNotIn("synthetic-private", stdout.getvalue())


if __name__ == "__main__": unittest.main()
