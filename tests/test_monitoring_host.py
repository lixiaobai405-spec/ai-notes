"""Synthetic host/Docker fixtures only; no daemon or real host is queried."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
host = importlib.import_module("monitoring_host")

ID1, ID2 = "a" * 64, "b" * 64


class FakeDocker:
    def __init__(self, records=None, stats=None):
        self.records = records if records is not None else [self.record()]
        self.stats = stats if stats is not None else [self.stat()]
        self.calls = []
        self.outputs = {}

    @staticmethod
    def record(identifier=ID1, service="api", **values):
        result = {"id": identifier, "project": "test-site", "service": service,
                  "oneoff": "False", "running": True, "state": "running", "health": "healthy"}
        result.update(values)
        return result

    @staticmethod
    def stat(identifier=ID1, **values):
        result = {"id": identifier, "cpu": "125.50%", "memory": "1.5MiB / 2GiB"}
        result.update(values)
        return result

    def __call__(self, command, *, timeout):
        self.calls.append((list(command), timeout))
        action = command[3]
        if action in self.outputs:
            output = self.outputs[action]
            if isinstance(output, Exception):
                raise output
            return output
        if action == "ps":
            service_filter = next(value for value in command if value.startswith("label=com.docker.compose.service="))
            service = service_filter.rsplit("=", 1)[1]
            return "\n".join(row["id"] for row in self.records if row["service"] == service).encode()
        if action == "inspect":
            identifiers = command[command.index("--format") + 2:]
            return "\n".join(json.dumps(row) for row in self.records if row["id"] in identifiers).encode()
        if action == "stats":
            identifiers = command[command.index("--format") + 2:]
            return "\n".join(json.dumps(row) for row in self.stats if row["id"] in identifiers).encode()
        raise AssertionError(command)


class MonitoringHostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc = Path(temp.name)
        (self.proc / "meminfo").write_text("MemTotal: 8192 kB\nMemAvailable: 2048 kB\n")
        self.runner = FakeDocker()
        self.disk = patch.object(host.shutil, "disk_usage", return_value=(1000, 750, 250))
        self.disk.start()
        self.addCleanup(self.disk.stop)
        self.detect = patch.object(host, "_in_container", return_value=False)
        self.detect.start()
        self.addCleanup(self.detect.stop)

    def collect(self, **changes):
        options = {"disks": {"data": str(self.proc)}, "project": "test-site", "services": ["api"],
                   "runner": self.runner, "proc_root": self.proc, "platform": "linux"}
        options.update(changes)
        return host.collect_host_metrics(**options)

    def service(self, result):
        return result["containers"]["services"]["api"]

    def test_complete_metrics_and_read_only_commands(self):
        result = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["memory"], {"status": "ok", "total_bytes": 8192 * 1024,
                                          "available_bytes": 2048 * 1024, "used_percent": 75.0})
        self.assertEqual(result["disks"]["data"], {"status": "ok", "total_bytes": 1000,
                                                  "free_bytes": 250, "used_percent": 75.0})
        service = self.service(result)
        self.assertEqual(service["status"], "ok")
        self.assertEqual(service["cpu_percent"], 125.5)
        self.assertEqual(service["memory_usage_bytes"], 1572864)
        self.assertEqual(service["memory_limit_bytes"], 2 * 1024 ** 3)
        self.assertEqual((service["running_count"], service["container_count"]), (1, 1))
        for command, timeout in self.runner.calls:
            self.assertEqual(command[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            self.assertIn(command[3], {"ps", "inspect", "stats"})
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)
            self.assertIn("--format", command)
            self.assertNotIn("{{json .}}", command)
        listing = self.runner.calls[0][0]
        self.assertIn("--all", listing)
        self.assertIn("label=com.docker.compose.project=test-site", listing)
        self.assertIn("label=com.docker.compose.service=api", listing)
        self.assertIn("label=com.docker.compose.oneoff=False", listing)
        projected = self.runner.calls[1][0]
        self.assertNotIn(".Config.Env", str(projected))
        self.assertNotIn(".Name", str(projected))
        summary = json.dumps(result)
        for value in (str(self.proc), ID1, "test-site", "docker.sock"):
            self.assertNotIn(value, summary)

    def test_missing_stopped_unhealthy_and_starting_are_not_ok(self):
        scenarios = [([], "missing", 0, 0),
                     ([FakeDocker.record(running=False, state="exited", health=None)], "stopped", 0, 1),
                     ([FakeDocker.record(health="unhealthy")], "unhealthy", 1, 1),
                     ([FakeDocker.record(health="starting")], "partial", 1, 1),
                     ([FakeDocker.record(state="paused")], "partial", 0, 1)]
        for records, status, running, count in scenarios:
            with self.subTest(status=status):
                self.runner = FakeDocker(records)
                result = self.collect()
                row = self.service(result)
                self.assertEqual(row["status"], status)
                self.assertEqual((row["running_count"], row["container_count"]), (running, count))
                self.assertEqual(result["containers"]["status"], "partial")
                self.assertEqual(result["status"], "partial")
                if not running:
                    self.assertIsNone(row["cpu_percent"])
                    self.assertFalse(any(command[3] == "stats" for command, _ in self.runner.calls))

    def test_absent_healthcheck_can_be_operational(self):
        self.runner.records[0]["health"] = None
        self.assertEqual(self.service(self.collect())["status"], "ok")

    def test_multiple_instances_are_ambiguous_and_resources_sum(self):
        self.runner = FakeDocker([FakeDocker.record(), FakeDocker.record(ID2)],
                                 [FakeDocker.stat(), FakeDocker.stat(ID2, cpu="25%", memory="512KiB / 1GiB")])
        row = self.service(self.collect())
        self.assertEqual(row["status"], "ambiguous")
        self.assertEqual((row["running_count"], row["container_count"]), (2, 2))
        self.assertEqual(row["cpu_percent"], 150.5)
        self.assertEqual(row["memory_usage_bytes"], 2 * 1024 ** 2)
        self.assertEqual(row["memory_limit_bytes"], 3 * 1024 ** 3)

    def test_inspect_rejects_wrong_identity_oneoff_state_and_extra_fields(self):
        for changes in ({"project": "private-project"}, {"service": "other"}, {"oneoff": "True"},
                        {"oneoff": None}, {"running": "true"}, {"state": "unknown"},
                        {"state": "exited"}, {"health": "unknown"}, {"env": "secret-sentinel"}):
            with self.subTest(changes=changes):
                self.runner = FakeDocker()
                wrong = FakeDocker.record(**changes)
                self.runner.outputs["inspect"] = json.dumps(wrong).encode()
                result = self.collect()
                row = self.service(result)
                self.assertEqual(row["status"], "unavailable")
                self.assertIsNone(row["container_count"])
                self.assertNotIn("secret-sentinel", json.dumps(result))
                self.assertFalse(any(command[3] == "stats" for command, _ in self.runner.calls))

    def test_ps_rejects_duplicate_short_ids_and_pagination(self):
        for output in ((ID1 + "\n" + ID1).encode(), ID1[:12].encode(),
                       (ID1 + "\nnext_page=2").encode(), b'{"containers":[],"next":"secret"}'):
            with self.subTest(output=output):
                self.runner.outputs["ps"] = output
                self.assertEqual(self.service(self.collect())["status"], "unavailable")
                self.assertEqual(self.runner.calls[-1][0][3], "ps")

    def test_inspect_missing_duplicate_and_json_duplicate_fields_rejected(self):
        row = json.dumps(FakeDocker.record()).encode()
        for output in (b"", row + b"\n" + row, row[:-1] + b',"id":"' + ID1.encode() + b'"}'):
            with self.subTest(output=output):
                self.runner.outputs["inspect"] = output
                self.assertEqual(self.service(self.collect())["status"], "unavailable")

    def test_stats_missing_wrong_duplicate_extra_fields_become_partial(self):
        row = json.dumps(FakeDocker.stat()).encode()
        for output in (b"", json.dumps(FakeDocker.stat(ID2)).encode(), row + b"\n" + row,
                       json.dumps(FakeDocker.stat(name="private-container-name")).encode()):
            with self.subTest(output=output):
                self.runner.outputs["stats"] = output
                result = self.collect()
                service = self.service(result)
                self.assertEqual(service["status"], "partial")
                self.assertEqual(service["container_count"], 1)
                self.assertIsNone(service["cpu_percent"])
                self.assertNotIn("private-container-name", json.dumps(result))

    def test_decimal_and_binary_memory_units(self):
        for value, expected in (("2KiB", 2048), ("1.5MiB", 1572864), ("2GiB", 2147483648),
                                ("1kB", 1000), ("1MB", 1000000), ("1GB", 1000000000), ("0B", 0)):
            with self.subTest(value=value):
                self.runner.stats = [FakeDocker.stat(memory=value + " / 4GiB")]
                row = self.service(self.collect())
                self.assertEqual(row["memory_usage_bytes"], expected)
                self.assertEqual(row["status"], "ok")

    def test_unknown_stats_do_not_turn_into_zero(self):
        for cpu, memory in (("N/A", "1MiB / 2GiB"), ("nan%", "1MiB / 2GiB"),
                            ("-1%", "1MiB / 2GiB"), ("1%", "1XB / 2GiB"),
                            ("1%", "1MiB / 0B"), ("1%", "unknown")):
            with self.subTest(cpu=cpu, memory=memory):
                self.runner.stats = [FakeDocker.stat(cpu=cpu, memory=memory)]
                row = self.service(self.collect())
                self.assertEqual(row["status"], "partial")
                self.assertTrue(any(row[key] is None for key in ("cpu_percent", "memory_usage_bytes", "memory_limit_bytes")))

    def test_docker_error_does_not_leak_process_details(self):
        for action in ("ps", "inspect", "stats"):
            with self.subTest(action=action):
                self.runner = FakeDocker()
                self.runner.outputs[action] = RuntimeError("secret-key-sentinel /private/path container-name")
                result = self.collect()
                self.assertNotEqual(self.service(result)["status"], "ok")
                self.assertNotIn("secret-key-sentinel", json.dumps(result))
                self.assertNotIn("/private/path", json.dumps(result))

    def test_per_call_and_aggregate_output_limits(self):
        self.runner.outputs["ps"] = b"x" * (host.MAX_OUTPUT_BYTES + 1)
        self.assertEqual(self.service(self.collect())["code"], "monitoring_host_output_limit")
        self.runner = FakeDocker()
        row = json.dumps(FakeDocker.record()).encode()
        self.runner.outputs["inspect"] = row + b" " * (host.MAX_OUTPUT_BYTES - len(row))
        self.assertEqual(self.service(self.collect())["code"], "monitoring_host_output_limit")

    def test_output_limit_prevents_further_service_processes(self):
        self.runner.outputs["ps"] = b"x" * (host.MAX_OUTPUT_BYTES + 1)
        result = self.collect(services=["api", "worker"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(result["containers"]["services"]["worker"]["code"], "monitoring_host_output_limit")

    def test_container_limit_rejects_truncated_inventory(self):
        self.runner.outputs["ps"] = "\n".join(f"{number:064x}" for number in range(host.MAX_CONTAINERS + 1)).encode()
        self.assertEqual(self.service(self.collect())["code"], "monitoring_host_container_limit")

    def test_total_deadline_is_shared(self):
        instant = [0.0]
        original = self.runner

        def slow(command, *, timeout):
            result = original(command, timeout=timeout)
            instant[0] += 0.6
            return result

        with patch.object(host.time, "monotonic", side_effect=lambda: instant[0]):
            result = self.collect(timeout_seconds=1, runner=slow)
        self.assertEqual(self.service(result)["status"], "unavailable")
        self.assertEqual(self.service(result)["code"], "monitoring_host_timeout")
        self.assertEqual(len(original.calls), 2)
        self.assertAlmostEqual(original.calls[1][1], 0.4)

    def test_stats_timeout_retains_identity_but_is_partial(self):
        self.runner.outputs["stats"] = TimeoutError("private-output")
        row = self.service(self.collect())
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["container_count"], 1)
        self.assertEqual(row["code"], "monitoring_host_timeout")

    def test_service_failures_are_isolated(self):
        result = self.collect(services=["api", "worker"])
        self.assertEqual(result["containers"]["services"]["api"]["status"], "ok")
        self.assertEqual(result["containers"]["services"]["worker"]["status"], "missing")
        self.assertEqual(result["containers"]["status"], "partial")

    def test_linux_memory_missing_malformed_duplicate_or_oversized(self):
        for value in ("MemTotal: 8192 kB\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
                      "MemTotal: 1 kB\nMemAvailable: 2 kB\n", "MemTotal: 8 MB\nMemAvailable: 2 kB\n",
                      "MemTotal: 8 kB\nMemTotal: 8 kB\nMemAvailable: 2 kB\n", "x" * (host.MAX_OUTPUT_BYTES + 1)):
            with self.subTest(value=value[:70]):
                (self.proc / "meminfo").write_text(value)
                memory = self.collect()["memory"]
                self.assertEqual(memory["status"], "unavailable")
                self.assertIsNone(memory["total_bytes"])

    def test_memory_and_disks_inside_container_are_not_host_metrics(self):
        with patch.object(host, "_in_container", return_value=True):
            result = self.collect()
        self.assertEqual(result["memory"]["status"], "unavailable")
        self.assertEqual(result["disks"]["data"]["status"], "unavailable")
        self.assertEqual(result["memory"]["code"], "monitoring_host_container_namespace")

    def test_unknown_container_detection_is_unavailable(self):
        with patch.object(host, "_in_container", return_value=None):
            result = self.collect()
        self.assertEqual(result["memory"]["status"], "unavailable")
        self.assertEqual(result["disks"]["data"]["status"], "unavailable")

    def test_non_linux_memory_and_docker_are_explicitly_unavailable(self):
        result = self.collect(platform="win32")
        self.assertEqual(result["memory"]["status"], "unavailable")
        self.assertEqual(result["containers"]["status"], "unavailable")
        self.assertFalse(self.runner.calls)

    def test_disk_failures_and_invalid_values_preserve_fixed_shape(self):
        for value in (PermissionError("private-filename"), (0, 0, 0), (100, 0, 101), (100, -1, 101)):
            with self.subTest(value=value):
                with patch.object(host.shutil, "disk_usage", side_effect=value if isinstance(value, Exception) else None,
                                  return_value=value):
                    result = self.collect()
                self.assertEqual(result["disks"]["data"]["status"], "unavailable")
                self.assertIsNone(result["disks"]["data"]["free_bytes"])
                self.assertNotIn("private-filename", json.dumps(result))

    def test_stalled_disk_is_bounded_by_the_shared_deadline(self):
        release = threading.Event()

        def stalled(path):
            release.wait(2)
            return 100, 75, 25

        started = time.monotonic()
        try:
            with patch.object(host.shutil, "disk_usage", side_effect=stalled):
                result = self.collect(timeout_seconds=0.03)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result["disks"]["data"]["code"], "monitoring_host_timeout")
        self.assertEqual(self.service(result)["code"], "monitoring_host_timeout")
        self.assertFalse(self.runner.calls)

    def test_default_runner_reuses_existing_bounded_process_adapter(self):
        process = importlib.import_module("site_backup_process")
        with patch.object(process, "execute", return_value=b"fixture") as execute:
            self.assertEqual(host._default_runner(["docker", "ps"], timeout=0.5), b"fixture")
        execute.assert_called_once_with(["docker", "ps"], cwd=ROOT, timeout=0.5)

    def test_invalid_config_fails_before_io(self):
        for changes in ({"disks": {"../private": "/"}}, {"services": ["api", "api"]},
                        {"project": "--context=remote"}, {"services": ["api;restart"]},
                        {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
                        {"timeout_seconds": True}, {"timeout_seconds": 10 ** 500},
                        {"disks": {"data": "\x00private"}}):
            with self.subTest(changes=changes):
                with self.assertRaises(host.MonitoringError) as raised:
                    self.collect(**changes)
                self.assertEqual(raised.exception.code, "monitoring_host_config_invalid")
                self.assertFalse(self.runner.calls)

    def test_container_detection_fixture_checks_cgroup_and_overlay(self):
        self.detect.stop()
        (self.proc / "self").mkdir()
        (self.proc / "1").mkdir()
        (self.proc / "self/cgroup").write_text("0::/user.slice\n")
        (self.proc / "1/cgroup").write_text("0::/init.scope\n")
        (self.proc / "self/mountinfo").write_text("1 0 8:1 / / rw - ext4 /dev/sda rw\n")
        self.assertFalse(host._in_container(self.proc))
        (self.proc / "self/cgroup").write_text("0::/docker/" + ID1 + "\n")
        self.assertTrue(host._in_container(self.proc))
        (self.proc / "self/cgroup").write_text("0::/\n")
        (self.proc / "self/mountinfo").write_text("1 0 0:1 / / rw - overlay overlay rw\n")
        self.assertTrue(host._in_container(self.proc))
        (self.proc / "self/cgroup").unlink()
        self.assertIsNone(host._in_container(self.proc))

    def test_malformed_mount_evidence_cannot_prove_host_provenance(self):
        self.detect.stop()
        (self.proc / "self").mkdir()
        (self.proc / "self/cgroup").write_text("0::/user.slice\n")
        (self.proc / "self/mountinfo").write_text("unrecognized-output\n")
        self.assertIsNone(host._in_container(self.proc))


if __name__ == "__main__":
    unittest.main()
