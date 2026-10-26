"""Host-side maintenance-window backups. No scheduling is installed by this CLI."""

import argparse
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal
import sys
import time
from uuid import uuid4

from site_backup_state import (ScheduleError, StateStore, private_path,
                               register_backup, retention_plan, prune_backups)
from site_backup_process import execute


MAX_OUTPUT = 64 * 1024
INSPECT = ('{"id":{{json .Id}},"service":{{json (index .Config.Labels '
           '"com.docker.compose.service")}},"project":{{json (index .Config.Labels '
           '"com.docker.compose.project")}},"running":{{json .State.Running}},'
           '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}')
ID = re.compile(r"[0-9a-f]{12,64}")
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")


class Interrupted(BaseException):
    """Do not let retention's conservative Exception handler swallow SIGTERM."""


def require(condition, code="schedule_configuration_invalid"):
    if not condition:
        raise ScheduleError(code)


def json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def utcnow():
    return datetime.now(timezone.utc)


def retention_summary(result):
    """Bound durable/log output even after years of backups or unknown folders."""
    summary = {}
    for key in ("deleted", "retained", "skipped", "candidates"):
        if key in result:
            summary[key + "_count"] = len(result[key])
            summary[key] = result[key][:20]
    return summary


@dataclass(frozen=True)
class Config:
    repository: Path
    python: Path
    docker: Path
    compose_files: tuple
    compose_env_file: Path
    project_name: str
    database_url_file: Path
    storage_backend: str
    storage_root: Path | None
    oss_env_file: Path | None
    backup_root: Path
    state_root: Path
    keep_last: int = 7
    maintenance_start_utc: str = "19:00"
    maintenance_minutes: int = 60
    backup_timeout: int = 1800
    command_timeout: int = 120
    restore_timeout: int = 120
    notification_command: tuple = ()
    notification_timeout: int = 15
    writers_exclusive_confirmed: bool = False


def load_config(path):
    path = private_path(path)
    require(path.is_file() and path.stat().st_size <= MAX_OUTPUT)
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=json_pairs)
    require(isinstance(data, dict) and set(data) <= set(Config.__dataclass_fields__))
    path_fields = {"repository", "python", "docker", "compose_env_file", "database_url_file",
                   "storage_root", "oss_env_file", "backup_root", "state_root"}
    for field in path_fields:
        value = data.get(field)
        if value is not None:
            require(isinstance(value, str) and Path(value).is_absolute())
            data[field] = private_path(value)
    files = data.get("compose_files")
    require(isinstance(files, list) and 1 <= len(files) <= 4)
    require(all(isinstance(value, str) and Path(value).is_absolute() for value in files))
    data["compose_files"] = tuple(private_path(value) for value in files)
    notify = data.get("notification_command", [])
    require(isinstance(notify, list) and len(notify) <= 10
            and all(isinstance(arg, str) and 0 < len(arg) <= 1024 and "\x00" not in arg for arg in notify))
    require(not notify or Path(notify[0]).is_absolute())
    if notify:
        private_path(notify[0])
    data["notification_command"] = tuple(notify)
    cfg = Config(**data)
    require(isinstance(cfg.project_name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", cfg.project_name))
    require(cfg.storage_backend in {"local", "oss"})
    require((cfg.storage_backend == "local" and cfg.storage_root is not None and cfg.oss_env_file is None)
            or (cfg.storage_backend == "oss" and cfg.oss_env_file is not None and cfg.storage_root is None))
    for field, minimum, maximum in (("keep_last", 1, 365), ("maintenance_minutes", 1, 180),
                                    ("backup_timeout", 1, 7200), ("command_timeout", 65, 300),
                                    ("restore_timeout", 1, 300), ("notification_timeout", 1, 60)):
        value = getattr(cfg, field)
        require(type(value) is int and minimum <= value <= maximum)
    require(type(cfg.writers_exclusive_confirmed) is bool)
    require(isinstance(cfg.maintenance_start_utc, str)
            and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", cfg.maintenance_start_utc))
    roots = [cfg.backup_root, cfg.state_root, cfg.repository]
    # Backup/state may live below the repository, but must not contain it.
    for root in roots[:2]:
        require(root != cfg.repository and not cfg.repository.is_relative_to(root))
    require(not cfg.backup_root.is_relative_to(cfg.state_root)
            and not cfg.state_root.is_relative_to(cfg.backup_root))
    if cfg.storage_root:
        for root in roots[:2]:
            require(not root.is_relative_to(cfg.storage_root) and not cfg.storage_root.is_relative_to(root))
    for secret_file in (cfg.compose_env_file, cfg.database_url_file, cfg.oss_env_file):
        if secret_file:
            require(not secret_file.is_relative_to(cfg.backup_root)
                    and not secret_file.is_relative_to(cfg.state_root))
    return cfg


def check_config(cfg):
    files = [cfg.python, cfg.docker, cfg.repository / "scripts/site_backup.py",
             cfg.compose_env_file, cfg.database_url_file, *cfg.compose_files]
    if cfg.oss_env_file:
        files.append(cfg.oss_env_file)
    if cfg.notification_command:
        files.append(Path(cfg.notification_command[0]))
    require(all(private_path(item).is_file() for item in files), "schedule_required_file_missing")
    require(cfg.writers_exclusive_confirmed, "schedule_maintenance_not_confirmed")


class Runner:
    def __init__(self, cfg, *, executor=execute, clock=utcnow, sleep=time.sleep):
        self.cfg, self.executor, self.clock, self.sleep = cfg, executor, clock, sleep
        self.store = StateStore(cfg.state_root)

    def call(self, arguments, timeout=None, payload=None):
        return self.executor(arguments, cwd=self.cfg.repository,
                             timeout=timeout or self.cfg.command_timeout, payload=payload)

    def docker(self, *arguments):
        return self.call([self.cfg.docker, *arguments])

    def inspect(self, identifier):
        require(isinstance(identifier, str) and ID.fullmatch(identifier), "schedule_container_invalid")
        value = json.loads(self.docker("inspect", "--format", INSPECT, identifier))
        require(isinstance(value, dict) and set(value) == {"id", "service", "project", "running", "health"}
                and value["id"] == identifier and value["project"] == self.cfg.project_name
                and type(value["running"]) is bool, "schedule_container_identity_changed")
        return value

    def discover(self):
        command = [self.cfg.docker, "compose", "--project-directory", self.cfg.repository,
                   "--project-name", self.cfg.project_name, "--env-file", self.cfg.compose_env_file]
        for path in self.cfg.compose_files:
            command.extend(["-f", path])
        identifiers = self.call([*command, "ps", "--all", "--quiet"]).decode("ascii").split()
        require(1 <= len(identifiers) <= 10 and len(set(identifiers)) == len(identifiers), "schedule_project_invalid")
        services = {}
        for identifier in identifiers:
            value = self.inspect(identifier)
            name = value["service"]
            require(name in {"db", "api", "worker", "web", "migrate"}
                    and name not in services, "schedule_project_invalid")
            require(name != "migrate" or not value["running"], "schedule_migration_running")
            services[name] = value
        require("db" in services and services["db"]["running"], "schedule_database_not_running")
        require("api" in services and "worker" in services, "schedule_project_invalid")
        return services

    def in_window(self):
        current = self.clock().astimezone(timezone.utc)
        hour, minute = map(int, self.cfg.maintenance_start_utc.split(":"))
        elapsed = (current.hour * 60 + current.minute - hour * 60 - minute) % 1440
        return elapsed < self.cfg.maintenance_minutes

    def phase(self, status, value):
        status["phase"] = value
        self.store.write("status.json", status)

    def recover_locked(self):
        record = self.store.read("recovery.json")
        if record is None:
            return {"restored": []}
        require(isinstance(record, dict) and set(record) == {"run_id", "services"}
                and isinstance(record["run_id"], str) and RUN_ID.fullmatch(record["run_id"])
                and isinstance(record["services"], list) and len(record["services"]) <= 2,
                "schedule_recovery_invalid")
        services = self.discover()
        restored = []
        names = set()
        for item in record["services"]:
            require(isinstance(item, dict) and set(item) == {"name", "id"}
                    and item["name"] in {"api", "worker"} and item["name"] not in names,
                    "schedule_recovery_invalid")
            name, identifier = item["name"], item["id"]
            names.add(name)
        failed = False
        identity_changed = False
        for item in record["services"]:
            if services[item["name"]]["id"] != item["id"]:
                identity_changed = True
                continue
            # A broken Worker must not prevent the API from coming back.
            try:
                identifier = item["id"]
                if not self.inspect(identifier)["running"]:
                    self.docker("start", identifier)
                deadline = time.monotonic() + self.cfg.restore_timeout
                while True:
                    value = self.inspect(identifier)
                    if value["running"] and (item["name"] != "api" or value["health"] == "healthy"):
                        break
                    require(time.monotonic() < deadline, "schedule_service_restore_failed")
                    self.sleep(1)
                restored.append(item["name"])
            except Exception:
                failed = True
        require(not identity_changed, "schedule_container_identity_changed")
        require(not failed, "schedule_service_restore_failed")
        self.store.write("recovery.json", None)
        return {"restored": restored}

    def recover(self):
        with self.store.lock():
            return self.recover_locked()

    def verify(self, destination):
        self.call([self.cfg.python, self.cfg.repository / "scripts/site_backup.py", "verify", destination],
                  timeout=self.cfg.backup_timeout)
        path = private_path(destination / "manifest.json")
        require(path.stat().st_size <= 32 * 1024**2, "schedule_manifest_invalid")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=json_pairs)

    def notify(self, event):
        if not self.cfg.notification_command:
            event["notification_status"] = "not_configured"
            self.store.write("events/" + event["event_id"] + ".json", event)
            return
        event["attempts"] += 1
        payload = {key: event[key] for key in ("event_id", "run_id", "created_at", "phase", "error_code")}
        try:
            self.call(self.cfg.notification_command, timeout=self.cfg.notification_timeout, payload=payload)
            event["notification_status"] = "delivered"
        except Exception:
            event["notification_status"] = "failed"
        self.store.write("events/" + event["event_id"] + ".json", event)

    def failure(self, status):
        event = {key: status[key] for key in ("run_id", "phase", "error_code")}
        event.update(event_id=uuid4().hex, created_at=self.clock().isoformat(), attempts=0,
                     notification_status="pending")
        self.store.write("events/" + event["event_id"] + ".json", event)
        self.notify(event)

    def retry_notifications(self):
        with self.store.lock():
            root = private_path(self.store.root / "events")
            pending = 0
            if root.exists():
                paths = sorted(root.iterdir())
                require(len(paths) <= 10000, "schedule_event_limit")
                for path in paths:
                    if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                        continue
                    event = self.store.read("events/" + path.name)
                    require(isinstance(event, dict) and set(event) == {"event_id", "run_id", "created_at", "phase", "error_code", "attempts", "notification_status"}
                            and event["event_id"] == path.stem and RUN_ID.fullmatch(event["run_id"])
                            and re.fullmatch(r"schedule_[a-z0-9_]{1,64}", event["error_code"])
                            and re.fullmatch(r"[a-z_]{1,32}", event["phase"])
                            and type(event["attempts"]) is int and 0 <= event["attempts"] <= 10000,
                            "schedule_event_invalid")
                    if event["notification_status"] != "delivered" and pending < 100:
                        self.notify(event)
                        pending += 1
            return {"attempted": pending}

    def run(self):
        with self.store.lock():
            run_id = self.clock().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
            status = {"run_id": run_id, "started_at": self.clock().isoformat(), "phase": "preflight",
                      "backup_verified": False, "services_restored": False, "ok": False}
            error = None
            recovery_owned = False
            try:
                require(self.store.read("recovery.json") is None, "schedule_recovery_required")
                self.phase(status, "preflight")
                check_config(self.cfg)
                require(self.in_window(), "schedule_outside_window")
                root = private_path(self.cfg.backup_root)
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_path(root)
                services = self.discover()
                originals = [{"name": name, "id": services[name]["id"]}
                             for name in ("worker", "api") if services[name]["running"]]
                self.store.write("recovery.json", {"run_id": run_id, "services": originals})
                recovery_owned = True
                self.phase(status, "stop")
                if originals:
                    self.docker("stop", "--time", "60", *[item["id"] for item in originals])
                stopped = self.discover()
                require(all(stopped[name]["id"] == services[name]["id"] and not stopped[name]["running"]
                            for name in ("api", "worker")), "schedule_writers_still_running")
                self.phase(status, "backup")
                destination = root / ("scheduled-" + run_id)
                command = [self.cfg.python, self.cfg.repository / "scripts/site_backup.py", "backup",
                           "--database-url-file", self.cfg.database_url_file,
                           "--postgres-container", services["db"]["id"],
                           "--storage-backend", self.cfg.storage_backend,
                           "--output", destination, "--maintenance-confirmed"]
                command.extend(["--storage-root", self.cfg.storage_root] if self.cfg.storage_backend == "local"
                               else ["--env-file", self.cfg.oss_env_file])
                self.call(command, timeout=self.cfg.backup_timeout)
                self.phase(status, "verify")
                manifest = self.verify(destination)
                status["backup_verified"] = True
            except (Exception, Interrupted) as caught:
                error = (caught if isinstance(caught, ScheduleError) else
                         ScheduleError("schedule_interrupted" if isinstance(caught, Interrupted) else "schedule_backup_failed"))
                status["failure_phase"] = status["phase"]
            finally:
                if recovery_owned:
                    # A full disk must not prevent an attempt to resume services.
                    with suppress(Exception):
                        self.phase(status, "restore")
                    try:
                        self.recover_locked()
                        status["services_restored"] = True
                    except Exception:
                        error = ScheduleError("schedule_service_restore_failed")
                        status["failure_phase"] = "restore"
            if error is None:
                try:
                    self.phase(status, "retention")
                    register_backup(self.store, root, destination.name, manifest)
                    status["retention"] = retention_summary(prune_backups(self.store, root, self.cfg.keep_last, self.verify))
                    status["ok"] = True
                except (Exception, Interrupted) as caught:
                    error = ScheduleError("schedule_interrupted" if isinstance(caught, Interrupted) else "schedule_retention_failed")
                    status["failure_phase"] = "retention"
            status["phase"] = status.pop("failure_phase", "complete")
            status["finished_at"] = self.clock().isoformat()
            if error:
                status["error_code"] = error.code
            if error:
                with suppress(Exception):
                    self.store.write("status.json", status)
                try:
                    self.failure(status)
                except Exception:
                    status["notification_error_code"] = "schedule_notification_state_failed"
                    with suppress(Exception):
                        self.store.write("status.json", status)
                raise error
            self.store.write("status.json", status)
            return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "status", "retention-plan", "recover-services", "retry-notifications"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    def interrupted(*_):
        raise Interrupted()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        cfg = load_config(args.config)
        if args.command == "check":
            check_config(cfg)
            result = {"ok": True}
        else:
            runner = Runner(cfg)
            if args.command == "run":
                result = runner.run()
            elif args.command == "status":
                result = runner.store.read("status.json", {"phase": "never_run"})
            elif args.command == "retention-plan":
                with runner.store.lock():
                    result = retention_summary(retention_plan(runner.store, cfg.backup_root, cfg.keep_last))
            elif args.command == "recover-services":
                result = runner.recover()
            else:
                result = runner.retry_notifications()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, Interrupted) as error:
        code = (error.code if isinstance(error, ScheduleError) else
                "schedule_interrupted" if isinstance(error, Interrupted) else "schedule_configuration_invalid")
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
