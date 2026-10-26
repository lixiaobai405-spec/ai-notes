"""Private read-only operator monitoring: check, sample, status. No notifications."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from scripts.file_gc_state import PrivateState
from scripts.monitoring_common import (MonitoringError, MAX_OUTPUT_BYTES, require,
                                      timestamp, unavailable)

SCHEMA = "ai-notes-monitor"
STATE_NAME = "monitor.json"
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
ALIAS = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
VARIABLE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
CONFIG_FIELDS = {"database_url_env", "state_root", "compose_project", "services", "disks",
                 "window_minutes", "sample_timeout_seconds", "stale_after_seconds", "thresholds"}
STATE_FIELDS = {"format", "version", "target", "database_target", "attempt", "snapshot", "active", "history"}
DATABASE_ROUTING_QUERY = {"host", "hostaddr", "port", "dbname", "service", "servicefile",
                          "target_session_attrs", "load_balance_hosts", "options"}
DATABASE_ROUTING_ENV = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE",
                        "PGSYSCONFDIR", "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS")


def utcnow():
    return datetime.now(timezone.utc)


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def database_target(database_url, routing_environment):
    """Parse locally and hash routing identity; never retain URL credentials."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    try:
        require(isinstance(database_url, str) and len(database_url) <= 8192, "monitoring_database_invalid")
        parsed = make_url(database_url)
        require(parsed.drivername in {"postgresql", "postgresql+psycopg"} and bool(parsed.database),
                "monitoring_database_invalid")
        # Both accepted URL spellings use psycopg in the actual collector.
        host = parsed.host or ""
        host = host if host.startswith("/") else host.lower()
        identity = {"driver": "postgresql+psycopg", "host": host,
                    "port": parsed.port or 5432, "database": parsed.database,
                    "query": {key: list(values) for key, values in parsed.normalized_query.items()
                              if key in DATABASE_ROUTING_QUERY},
                    "environment": routing_environment}
        return fingerprint(identity)
    except (ValueError, TypeError, SQLAlchemyError):
        raise MonitoringError("monitoring_database_invalid") from None


def validate_config(cfg):
    from scripts.monitoring_alerts import validate_thresholds
    require(isinstance(cfg, dict) and set(cfg) == CONFIG_FIELDS, "monitoring_config_invalid")
    require(isinstance(cfg["database_url_env"], str) and VARIABLE.fullmatch(cfg["database_url_env"]), "monitoring_config_invalid")
    require(isinstance(cfg["state_root"], str) and Path(cfg["state_root"]).is_absolute(), "monitoring_config_invalid")
    require(isinstance(cfg["compose_project"], str) and PROJECT.fullmatch(cfg["compose_project"]), "monitoring_config_invalid")
    services = cfg["services"]
    require(isinstance(services, list) and 1 <= len(services) <= 16
            and all(isinstance(s, str) and NAME.fullmatch(s) for s in services)
            and len(set(services)) == len(services), "monitoring_config_invalid")
    disks = cfg["disks"]
    require(isinstance(disks, dict) and 1 <= len(disks) <= 8
            and all(isinstance(alias, str) and ALIAS.fullmatch(alias)
                    and isinstance(path, str) and Path(path).is_absolute() for alias, path in disks.items()), "monitoring_config_invalid")
    for field, low, high in (("window_minutes", 1, 1440), ("sample_timeout_seconds", 5, 60), ("stale_after_seconds", 60, 3600)):
        require(type(cfg[field]) is int and low <= cfg[field] <= high, "monitoring_config_invalid")
    require(cfg["stale_after_seconds"] > cfg["sample_timeout_seconds"], "monitoring_config_invalid")
    validate_thresholds(cfg["thresholds"])
    required = {"host.memory.used_percent", "database.connections.used_percent", "database.jobs.oldest_ready_seconds", "database.ai.oldest_queued_seconds"}
    required.update("host.disks." + alias + ".used_percent" for alias in disks)
    require(required <= {row["metric"] for row in cfg["thresholds"]}, "monitoring_thresholds_missing")
    for row in cfg["thresholds"]:
        if row["metric"].endswith("used_percent"):
            require(row["critical"] <= 100, "monitoring_config_invalid")
        if row["metric"].startswith("host.disks."):
            require(row["metric"].split(".")[2] in disks, "monitoring_config_invalid")
    return cfg


def safe_document(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
        require(len(raw) <= MAX_OUTPUT_BYTES, "monitoring_output_limit")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MonitoringError("monitoring_state_invalid") from None
    return value


def read_config(path):
    file = Path(path).absolute()
    try:
        cfg = PrivateState(file.parent).read(file.name)
    except Exception:
        raise MonitoringError("monitoring_config_unavailable") from None
    return validate_config(cfg)


class SiteMonitor:
    def __init__(self, config, *, state=None, host_collector=None, database_collector=None, now=utcnow):
        self.config = validate_config(config)
        try:
            self.state = state or PrivateState(config["state_root"])
        except Exception:
            raise MonitoringError("monitoring_state_unavailable") from None
        self.host_collector, self.database_collector, self.now = host_collector, database_collector, now
        self.target = fingerprint({key: config[key] for key in ("database_url_env", "compose_project", "services", "disks")})
        self.policy = fingerprint({key: config[key] for key in ("thresholds", "window_minutes")})

    def database_context(self):
        # Read once per operation, never during construction/check. Parsing the
        # configured URL cannot connect to PostgreSQL or load application settings.
        secret = os.environ.get(self.config["database_url_env"], "")
        if not secret:
            return secret, None, "monitoring_database_environment_missing"
        try:
            # Conservative identity: route defaults are included even when an
            # explicit URL overrides them. PGUSER/PGPASSWORD are credentials.
            routing = {key: os.environ.get(key, "") for key in DATABASE_ROUTING_ENV}
            return secret, database_target(secret, routing), None
        except MonitoringError as error:
            return secret, None, error.code

    def read(self):
        try:
            value = self.state.read(STATE_NAME)
        except FileNotFoundError:
            return None
        except Exception as error:
            # PrivateState normalizes missing/read errors, so only an actual
            # absent path can be treated as a fresh monitor.
            if not (self.state.root / STATE_NAME).exists():
                return None
            raise MonitoringError("monitoring_state_unavailable") from None
        require(isinstance(value, dict) and set(value) == STATE_FIELDS
                and value["format"] == SCHEMA and type(value["version"]) is int and value["version"] == 1,
                "monitoring_state_invalid")
        require(isinstance(value["target"], str) and re.fullmatch(r"[0-9a-f]{64}", value["target"]), "monitoring_state_invalid")
        require(value["database_target"] is None or isinstance(value["database_target"], str)
                and re.fullmatch(r"[0-9a-f]{64}", value["database_target"]), "monitoring_state_invalid")
        require(isinstance(value["active"], dict) and isinstance(value["history"], list)
                and isinstance(value["attempt"], dict)
                and (value["snapshot"] is None or isinstance(value["snapshot"], dict)), "monitoring_state_invalid")
        require(len(value["active"]) <= 128 and len(value["history"]) <= 100
                and value["attempt"].get("status") in {"running", "complete"}, "monitoring_state_invalid")
        timestamp(value["attempt"].get("started_at"))
        if value["attempt"]["status"] == "complete":
            timestamp(value["attempt"].get("finished_at"))
        if value["snapshot"] is not None:
            snapshot = value["snapshot"]
            require(set(snapshot) == {"observed_at", "window_minutes", "policy", "metrics", "evaluation"}
                    and isinstance(snapshot["metrics"], dict) and isinstance(snapshot["evaluation"], dict)
                    and snapshot["evaluation"].get("status") in {"ok", "warning", "critical", "unknown"}, "monitoring_state_invalid")
            timestamp(snapshot["observed_at"])
        safe_document(value)
        return value

    def write(self, value):
        try:
            self.state.write(STATE_NAME, safe_document(value))
        except MonitoringError:
            raise
        except Exception:
            raise MonitoringError("monitoring_state_unavailable") from None

    def remaining(self, started):
        return max(0, self.config["sample_timeout_seconds"] - (time.monotonic() - started))

    @staticmethod
    def collect(function, *args, **kwargs):
        try:
            value = function(*args, **kwargs)
            require(isinstance(value, dict) and value.get("status") in {"ok", "partial", "unavailable"}, "monitoring_collector_invalid")
            return safe_document(value)
        except Exception:
            return unavailable("monitoring_collection_failed")

    def sample(self):
        from scripts.monitoring_alerts import evaluate, transition
        from scripts.monitoring_database import collect_database_metrics
        from scripts.monitoring_host import collect_host_metrics
        started, observed_time = time.monotonic(), self.now()
        observed = observed_time.isoformat()
        try:
            with self.state.lock():
                previous = self.read() or {"format": SCHEMA, "version": 1, "attempt": {},
                                          "target": self.target, "database_target": None,
                                          "snapshot": None, "active": {}, "history": []}
                require(previous["target"] == self.target, "monitoring_target_changed")
                secret, current_database_target, database_code = self.database_context()
                require(current_database_target is None or previous["database_target"] is None
                        or previous["database_target"] == current_database_target, "monitoring_target_changed")
                previous["attempt"] = {"status": "running", "started_at": observed, "finished_at": None}
                self.write(previous)
                host = self.collect(self.host_collector or collect_host_metrics,
                    self.config["disks"], self.config["compose_project"], self.config["services"],
                    timeout_seconds=max(1, min(10, self.remaining(started))))
                if database_code:
                    database = unavailable(database_code)
                elif self.remaining(started) < 2:
                    database = unavailable("monitoring_timeout")
                else:
                    database = self.collect(self.database_collector or collect_database_metrics,
                        secret, window_minutes=self.config["window_minutes"],
                        timeout_seconds=int(min(10, self.remaining(started))), now=observed_time)
                metrics = {"host": host, "database": database}
                evaluation = evaluate(metrics, self.config["thresholds"])
                changes = transition(previous["active"], previous["history"], evaluation, observed)
                value = {"format": SCHEMA, "version": 1,
                    "target": self.target,
                    "database_target": current_database_target or previous["database_target"],
                    "attempt": {"status": "complete", "started_at": observed, "finished_at": self.now().isoformat()},
                    "snapshot": {"observed_at": observed, "window_minutes": self.config["window_minutes"], "policy": self.policy,
                                 "metrics": metrics, "evaluation": evaluation},
                    "active": changes["active"], "history": changes["history"]}
                self.write(value)
                return self.summary(value, current_database_target, database_code)
        except MonitoringError:
            raise
        except Exception as error:
            if getattr(error, "code", None) == "gc_locked":
                raise MonitoringError("monitoring_locked") from None
            raise MonitoringError("monitoring_sample_failed") from None

    def summary(self, value, current_database_target, database_code):
        snapshot = value["snapshot"]
        if not snapshot:
            return {"status": "unknown", "code": "monitoring_snapshot_missing", "active_alerts": len(value["active"])}
        age = (self.now() - timestamp(snapshot["observed_at"])).total_seconds()
        result = {"status": snapshot["evaluation"]["status"], "observed_at": snapshot["observed_at"],
                  "age_seconds": round(max(0, age), 3), "active_alerts": len(value["active"]),
                  "active_critical": sum(item.get("severity") == "critical" for item in value["active"].values()),
                  "history_events": len(value["history"])}
        ranks = {"ok": 0, "warning": 1, "unknown": 2, "critical": 3}
        for item in value["active"].values():
            severity = item.get("severity")
            require(severity in ranks and severity != "ok", "monitoring_state_invalid")
            if ranks[severity] > ranks[result["status"]]: result["status"] = severity
        if value["target"] != self.target:
            result.update(status="unknown", code="monitoring_target_changed")
        elif database_code:
            result.update(status="unknown", code=database_code)
        elif value["database_target"] != current_database_target:
            result.update(status="unknown", code="monitoring_target_changed")
        elif snapshot.get("policy") != self.policy:
            result.update(status="unknown", code="monitoring_policy_changed")
        elif value["attempt"].get("status") != "complete":
            result.update(status="unknown", code="monitoring_sample_incomplete")
        elif age < -5:
            result.update(status="unknown", code="monitoring_clock_changed")
        elif age > self.config["stale_after_seconds"]:
            result.update(status="unknown", code="monitoring_snapshot_stale")
        return result

    def status(self):
        value = self.read()
        if not value:
            return {"status": "unknown", "code": "monitoring_snapshot_missing", "active_alerts": 0}
        _, current_database_target, database_code = self.database_context()
        return self.summary(value, current_database_target, database_code)


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立只读监测；仅私密本机快照和告警，无外发/修复")
    parser.add_argument("command", choices=("check", "sample", "status"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        cfg = read_config(args.config)
        monitor = SiteMonitor(cfg)
        if args.command == "check":
            result = {"status": "valid", "services": len(cfg["services"]), "disks": len(cfg["disks"]),
                      "stale_after_seconds": cfg["stale_after_seconds"], "window_minutes": cfg["window_minutes"]}
        else:
            result = monitor.sample() if args.command == "sample" else monitor.status()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        # A stored sample is not a promise of business health. `status` is the
        # explicit threshold/staleness gate for operator automation.
        return 0 if args.command != "status" or result["status"] == "ok" else 1 if result["status"] == "warning" else 2
    except MonitoringError as error:
        print(json.dumps({"status": "unknown", "code": error.code}))
        return 3
    except Exception:
        print(json.dumps({"status": "unknown", "code": "monitoring_operation_failed"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
