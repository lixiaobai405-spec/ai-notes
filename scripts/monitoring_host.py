"""Bounded, private, read-only host and local Compose resource summaries.

The Linux host is the deployment target. Synthetic proc roots, platform values
and a runner are injection points for offline tests, not host verification.
Docker stats memory has the Docker CLI's cache-subtracted semantics.
"""

import json
import math
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time

try:
    from .monitoring_common import MAX_OUTPUT_BYTES, MonitoringError, require
except ImportError:
    from monitoring_common import MAX_OUTPUT_BYTES, MonitoringError, require


MAX_CONTAINERS = 64
MAX_SERVICES = 16
MAX_DISKS = 16
MAX_BYTES = 2 ** 63 - 1
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_ID = re.compile(r"[a-f0-9]{64}\Z")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
_DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
_INSPECT = (
    '{"id":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
    '"running":{{json .State.Running}},"state":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
)
_STATS = '{"id":{{json .Container}},"cpu":{{json .CPUPerc}},"memory":{{json .MemUsage}}}'
_RESOURCE_KEYS = ("cpu_percent", "memory_usage_bytes", "memory_limit_bytes", "memory_used_percent")


def _unknown(fields, code):
    return {"status": "unavailable", **dict.fromkeys(fields), "code": code}


def _service_unknown(code):
    return _unknown(("running_count", "container_count", *_RESOURCE_KEYS), code)


def _group_status(statuses):
    values = list(statuses)
    if values and all(value == "ok" for value in values):
        return "ok"
    if not values or all(value == "unavailable" for value in values):
        return "unavailable"
    return "partial"


def _default_runner(command, *, timeout):
    # The older process helper has a local absolute import. Keep that adapter
    # here, without changing the previously delivered scheduler implementation.
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from site_backup_process import execute
    return execute(command, cwd=Path(__file__).resolve().parents[1], timeout=timeout)


class _Budget:
    def __init__(self, seconds, runner):
        self.deadline = time.monotonic() + seconds
        self.runner = runner
        self.output_bytes = 0
        self.containers = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "monitoring_host_timeout")
        return remaining

    def run(self, arguments):
        timeout = self.remaining()
        require(self.output_bytes < MAX_OUTPUT_BYTES, "monitoring_host_output_limit")
        try:
            output = self.runner([*_DOCKER, *arguments], timeout=timeout)
        except Exception as error:
            code = getattr(error, "code", None)
            if isinstance(error, TimeoutError) or code == "schedule_process_timeout":
                raise MonitoringError("monitoring_host_timeout") from None
            if code == "schedule_process_output_limit":
                raise MonitoringError("monitoring_host_output_limit") from None
            raise MonitoringError("monitoring_docker_unavailable") from None
        self.remaining()
        require(isinstance(output, bytes), "monitoring_docker_output_invalid")
        self.output_bytes += len(output)
        require(self.output_bytes <= MAX_OUTPUT_BYTES, "monitoring_host_output_limit")
        try:
            return output.decode("utf-8", errors="strict")
        except UnicodeError:
            raise MonitoringError("monitoring_docker_output_invalid") from None


def _read_bounded(path):
    with Path(path).open("rb") as stream:
        value = stream.read(MAX_OUTPUT_BYTES + 1)
    require(len(value) <= MAX_OUTPUT_BYTES, "monitoring_host_output_limit")
    return value.decode("ascii", errors="strict")


def _in_container(proc_root):
    """Return None if host provenance cannot be established conservatively.

    This private function can be patched in tests. On the real proc root,
    Docker/Podman markers and cgroup/mount evidence are checked. An overlay
    root is intentionally treated conservatively, even on an overlay host.
    """
    root = Path(proc_root)
    try:
        if str(root) == "/proc":
            if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
                return True
            if Path("/run/systemd/container").exists():
                return True
        cgroup = _read_bounded(root / "self/cgroup")
        mounts = _read_bounded(root / "self/mountinfo")
        try:
            cgroup += "\n" + _read_bounded(root / "1/cgroup")
        except FileNotFoundError:
            pass
        if re.search(r"docker|kubepods|containerd|libpod|lxc", cgroup, re.IGNORECASE):
            return True
        root_mount = False
        for line in mounts.splitlines():
            before, separator, after = line.partition(" - ")
            fields, filesystem = before.split(), after.split()
            if separator and len(fields) >= 5 and fields[4] == "/" and filesystem:
                root_mount = True
                if filesystem[0] in {"overlay", "aufs"}:
                    return True
        if not cgroup.strip() or not root_mount:
            return None
        return False
    except (OSError, ValueError, UnicodeError):
        return None


def _memory(proc_root, budget):
    fields = ("total_bytes", "available_bytes", "used_percent")
    try:
        budget.remaining()
        value = _read_bounded(Path(proc_root) / "meminfo")
        memory = {}
        for line in value.splitlines():
            key, separator, raw = line.partition(":")
            if key not in {"MemTotal", "MemAvailable"}:
                continue
            require(separator and key not in memory, "monitoring_memory_invalid")
            match = re.fullmatch(r"\s*([0-9]{1,18})\s+kB\s*", raw)
            require(match is not None, "monitoring_memory_invalid")
            memory[key] = int(match.group(1)) * 1024
        total, available = memory.get("MemTotal"), memory.get("MemAvailable")
        require(total is not None and available is not None and 0 < total <= MAX_BYTES and 0 <= available <= total,
                "monitoring_memory_invalid")
        budget.remaining()
        return {"status": "ok", "total_bytes": total, "available_bytes": available,
                "used_percent": (total - available) / total * 100}
    except MonitoringError as error:
        return _unknown(fields, error.code)
    except (OSError, ValueError, UnicodeError):
        return _unknown(fields, "monitoring_memory_unavailable")


def _disk(path, budget):
    fields = ("total_bytes", "free_bytes", "used_percent")
    try:
        remaining = budget.remaining()
        measured = queue.Queue(maxsize=1)

        def measure():
            # A stalled network filesystem must not hold up the monitor. There
            # can be at most one such daemon worker after the budget expires.
            try:
                measured.put((True, shutil.disk_usage(path)))
            except Exception:
                measured.put((False, None))

        threading.Thread(target=measure, daemon=True, name="monitoring-disk").start()
        try:
            success, usage = measured.get(timeout=remaining)
        except queue.Empty:
            raise MonitoringError("monitoring_host_timeout") from None
        require(success, "monitoring_disk_unavailable")
        total, used, free = usage
        require(all(type(value) is int and 0 <= value <= MAX_BYTES for value in (total, used, free))
                and total > 0 and free <= total and used <= total, "monitoring_disk_invalid")
        budget.remaining()
        return {"status": "ok", "total_bytes": total, "free_bytes": free,
                "used_percent": (total - free) / total * 100}
    except MonitoringError as error:
        return _unknown(fields, error.code)
    except (OSError, ValueError, TypeError):
        return _unknown(fields, "monitoring_disk_unavailable")


def _json_rows(text, fields):
    def no_duplicates(pairs):
        values = {}
        for key, value in pairs:
            require(key not in values, "monitoring_docker_output_invalid")
            values[key] = value
        return values

    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        require(len(rows) < MAX_CONTAINERS, "monitoring_host_container_limit")
        try:
            row = json.loads(line, object_pairs_hook=no_duplicates,
                             parse_constant=lambda value: None)
        except (ValueError, TypeError, RecursionError):
            raise MonitoringError("monitoring_docker_output_invalid") from None
        require(type(row) is dict and set(row) == fields, "monitoring_docker_output_invalid")
        rows.append(row)
    return rows


def _identifiers(value, budget):
    identifiers = value.splitlines()
    require(all(_ID.fullmatch(value) for value in identifiers), "monitoring_docker_identity_invalid")
    require(len(set(identifiers)) == len(identifiers), "monitoring_docker_identity_invalid")
    budget.containers += len(identifiers)
    require(budget.containers <= MAX_CONTAINERS, "monitoring_host_container_limit")
    return identifiers


def _inspect(value, identifiers, project, service):
    rows = _json_rows(value, {"id", "project", "service", "oneoff", "running", "state", "health"})
    require(len(rows) == len(identifiers), "monitoring_docker_identity_invalid")
    seen = set()
    for row in rows:
        identifier = row["id"]
        require(type(identifier) is str and identifier in identifiers and identifier not in seen,
                "monitoring_docker_identity_invalid")
        require(row["project"] == project and row["service"] == service
                and type(row["oneoff"]) is str and row["oneoff"].lower() == "false",
                "monitoring_docker_identity_invalid")
        require(type(row["running"]) is bool and row["state"] in
                {"running", "paused", "restarting", "exited", "created", "dead", "removing"}
                and row["health"] in {None, "healthy", "unhealthy", "starting"},
                "monitoring_docker_state_invalid")
        require(row["running"] == (row["state"] in {"running", "paused", "restarting"}),
                "monitoring_docker_state_invalid")
        seen.add(identifier)
    return rows


def _percent(value):
    if not isinstance(value, str) or not value.endswith("%") or len(value) > 32:
        return None
    number = value[:-1].strip()
    if not _NUMBER.fullmatch(number):
        return None
    number = float(number)
    return number if math.isfinite(number) and number >= 0 else None


def _bytes(value):
    if not isinstance(value, str) or len(value) > 48:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)\s*", value)
    if not match:
        return None
    number, unit = match.groups()
    powers = {"B": 1, "kB": 1000, "KB": 1000,
              **{prefix + "B": 1000 ** power for power, prefix in enumerate("M G T P".split(), 2)},
              **{prefix + "iB": 1024 ** power for power, prefix in enumerate("K M G T P".split(), 1)}}
    number = float(number) * powers[unit]
    return round(number) if math.isfinite(number) and 0 <= number <= MAX_BYTES else None


def _stats(value, identifiers):
    rows = _json_rows(value, {"id", "cpu", "memory"})
    require(len(rows) == len(identifiers), "monitoring_docker_stats_invalid")
    seen, results = set(), []
    for row in rows:
        identifier = row["id"]
        require(type(identifier) is str and identifier in identifiers and identifier not in seen,
                "monitoring_docker_stats_invalid")
        seen.add(identifier)
        parts = row["memory"].split("/") if type(row["memory"]) is str else []
        usage = _bytes(parts[0]) if len(parts) == 2 else None
        limit = _bytes(parts[1]) if len(parts) == 2 else None
        limit = limit if limit is not None and limit > 0 else None
        results.append({"cpu_percent": _percent(row["cpu"]), "memory_usage_bytes": usage,
                        "memory_limit_bytes": limit})
    result = {}
    for key in _RESOURCE_KEYS[:-1]:
        values = [row[key] for row in results]
        result[key] = sum(values) if all(value is not None for value in values) else None
    usage, limit = result["memory_usage_bytes"], result["memory_limit_bytes"]
    result["memory_used_percent"] = usage / limit * 100 if usage is not None and limit else None
    return result


def _collect_service(project, service, budget):
    identifiers = _identifiers(budget.run([
        "ps", "--all", "--no-trunc", "--last", str(MAX_CONTAINERS + 1),
        "--filter", "label=com.docker.compose.project=" + project,
        "--filter", "label=com.docker.compose.service=" + service,
        # Compose's canonical label values are True/False, case-sensitive.
        "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}",
    ]), budget)
    result = {"status": "missing", "running_count": 0, "container_count": 0,
              **dict.fromkeys(_RESOURCE_KEYS)}
    if not identifiers:
        return result
    records = _inspect(budget.run(["inspect", "--format", _INSPECT, *identifiers]), identifiers, project, service)
    active = [row for row in records if row["state"] == "running"]
    result.update(running_count=len(active), container_count=len(records))
    if len(records) > 1:
        result["status"] = "ambiguous"
    elif records[0]["state"] in {"paused", "restarting"}:
        result["status"] = "partial"
    elif not active:
        result["status"] = "stopped"
    elif active[0]["health"] == "unhealthy":
        result["status"] = "unhealthy"
    elif active[0]["health"] == "starting":
        result["status"] = "partial"
    else:
        result["status"] = "ok"
    if active:
        try:
            running = [row["id"] for row in active]
            result.update(_stats(budget.run(["stats", "--no-stream", "--no-trunc", "--format", _STATS, *running]), running))
            if any(result[key] is None for key in _RESOURCE_KEYS):
                result["code"] = "monitoring_docker_stats_incomplete"
                if result["status"] == "ok":
                    result["status"] = "partial"
        except MonitoringError as error:
            result["code"] = error.code
            if result["status"] == "ok":
                result["status"] = "partial"
    return result


def collect_host_metrics(disks, project, services, *, timeout_seconds=10, runner=None,
                         proc_root="/proc", platform=None):
    """Return aggregate statuses and private metric values; unknown values are None.

    timeout_seconds accepts a finite positive int/float (at most 300 seconds).
    Every Docker call receives the remaining shared budget, and the total CLI
    output is limited to 64 KiB. No daemon operation runs on non-Linux systems.
    """
    require(type(disks) is dict and len(disks) <= MAX_DISKS and
            all(type(alias) is str and _ALIAS.fullmatch(alias) and type(path) is str and
                0 < len(path) <= 4096 and not any(ord(char) < 32 for char in path)
                for alias, path in disks.items()), "monitoring_host_config_invalid")
    require(type(project) is str and _PROJECT.fullmatch(project) and type(services) is list
            and len(services) <= MAX_SERVICES and all(type(value) is str and _SERVICE.fullmatch(value) for value in services)
            and len(set(services)) == len(services), "monitoring_host_config_invalid")
    require(type(timeout_seconds) in {int, float} and 0 < timeout_seconds <= 300
            and math.isfinite(timeout_seconds) and (runner is None or callable(runner)), "monitoring_host_config_invalid")
    require(platform is None or type(platform) is str, "monitoring_host_config_invalid")
    require(isinstance(proc_root, (str, Path)), "monitoring_host_config_invalid")
    budget = _Budget(timeout_seconds, runner or _default_runner)
    linux = (platform or sys.platform).lower().startswith("linux")
    provenance = _in_container(proc_root) if linux else False
    reason = ("monitoring_host_platform_unsupported" if not linux else
              "monitoring_host_container_namespace" if provenance else "monitoring_host_provenance_unknown")
    memory = (_memory(proc_root, budget) if linux and provenance is False else
              _unknown(("total_bytes", "available_bytes", "used_percent"), reason))
    filesystem = {alias: (_disk(path, budget) if provenance is False else
                          _unknown(("total_bytes", "free_bytes", "used_percent"), reason))
                  for alias, path in disks.items()}
    summaries = {}
    for service in services:
        try:
            summaries[service] = (_collect_service(project, service, budget) if linux else
                                  _service_unknown("monitoring_host_platform_unsupported"))
        except MonitoringError as error:
            summaries[service] = _service_unknown(error.code)
        except (OSError, ValueError, TypeError, OverflowError):
            summaries[service] = _service_unknown("monitoring_docker_output_invalid")
    containers = {"status": _group_status(row["status"] for row in summaries.values()), "services": summaries}
    return {"status": _group_status([memory["status"], *(row["status"] for row in filesystem.values()), containers["status"]]),
            "memory": memory, "disks": filesystem, "containers": containers}
