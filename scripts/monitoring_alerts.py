"""Pure, bounded threshold evaluation and local alert event transitions.

Only allowlisted aggregate metric paths and fixed diagnostic codes cross this
module. No collector output, exception text, process or network access is used.
``observed_keys`` identifies trusted current observations for recovery; a
missing value or an unknown observation can never resolve a previous alert.
"""

import math
import re
import uuid

try:
    from scripts.monitoring_common import MAX_HISTORY, require, timestamp
except ModuleNotFoundError as error:
    if error.name not in {"scripts", "scripts.monitoring_common"}:
        raise
    from monitoring_common import MAX_HISTORY, require, timestamp


MAX_ACTIVE = 128
_ALIAS = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_DISK_METRIC = re.compile(rf"host\.disks\.{_ALIAS}\.used_percent")
_SERVICE_KEY = re.compile(rf"host\.containers\.services\.{_ALIAS}")
_DISK_KEY = re.compile(rf"host\.disks\.{_ALIAS}")
_JOBS = ("ready_queued", "scheduled_queued", "running", "failed_total",
         "failed_created_window", "oldest_ready_seconds", "expired_leases")
_AI = ("queued", "running", "errors_finished_window", "provider_errors_finished_window",
       "oldest_queued_seconds", "expired_leases")
_UPLOADS = ("created_window", "expired_created_window", "rejected_created_window",
            "processing_failed_created_window", "pdf_rejected_created_window", "http_failure_rate")
_USAGE = ("known_total_tokens", "terminal_runs_window", "missing_usage_runs",
          "incomplete_usage_runs", "cost")
_FIXED_METRICS = {"host.memory.used_percent", "database.connections.used_percent"}
for _name, _fields in (("jobs", _JOBS), ("ai", _AI), ("uploads", _UPLOADS), ("usage", _USAGE)):
    _FIXED_METRICS.update(f"database.{_name}.{field}" for field in _fields)
_GROUP_KEYS = {"host.memory", "host.disks", "host.containers", "database.connections",
               "database.jobs", "database.ai", "database.uploads", "database.usage"}
_OPTIONAL = {"database.uploads.http_failure_rate", "database.usage.cost"}
_QUEUE_AGES = {"database.jobs.oldest_ready_seconds": "ready_queued",
               "database.ai.oldest_queued_seconds": "queued"}
_SEVERITIES = {"warning", "critical", "unknown"}
_INCIDENT_LEVEL = {"unknown": 0, "warning": 1, "critical": 2}
_CODES = {"metric_warning", "metric_critical", "metric_unavailable", "required_unavailable",
          "required_partial", "service_missing", "service_stopped", "service_unhealthy",
          "service_ambiguous", "service_unavailable", "usage_missing", "usage_incomplete"}
_EVENT_FIELDS = {"id", "key", "severity", "code", "value", "threshold",
                 "state", "first_seen", "last_seen"}
_ALERT_FIELDS = {"key", "severity", "code", "value", "threshold"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _metric_key(value):
    return isinstance(value, str) and (value in _FIXED_METRICS or bool(_DISK_METRIC.fullmatch(value)))


def _event_key(value):
    return isinstance(value, str) and (value in _GROUP_KEYS or _metric_key(value)
                                     or bool(_DISK_KEY.fullmatch(value))
                                     or bool(_SERVICE_KEY.fullmatch(value)))


def validate_thresholds(thresholds):
    """Validate and copy explicit rules, without guessing unconfigured limits."""
    require(isinstance(thresholds, list) and len(thresholds) <= MAX_ACTIVE, "monitoring_thresholds_invalid")
    result, seen = [], set()
    for rule in thresholds:
        require(isinstance(rule, dict) and set(rule) == {"metric", "warning", "critical"},
                "monitoring_thresholds_invalid")
        key = rule["metric"]
        require(_metric_key(key) and key not in seen and _number(rule["warning"])
                and _number(rule["critical"]) and rule["warning"] <= rule["critical"],
                "monitoring_thresholds_invalid")
        seen.add(key)
        result.append(dict(rule))
    return result


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _status(source):
    value = source.get("status")
    return value if isinstance(value, str) else None


def _source(metrics, key):
    parts = key.split(".")
    value = metrics
    for part in parts[:-1]:
        value = _mapping(value).get(part)
    return _mapping(value), parts[-1]


def evaluate(metrics, thresholds):
    """Return sanitized alerts and trustworthy keys, using no hidden defaults.

    Overall precedence is critical, unknown, warning, ok. Optional cost and HTTP
    failure rate stay explicitly unavailable when no authoritative source exists.
    """
    rules = validate_thresholds(thresholds)
    metrics = _mapping(metrics)
    host, database = _mapping(metrics.get("host")), _mapping(metrics.get("database"))
    alerts, observed, optional = {}, set(), []

    def add(key, severity, code, value=None, threshold=None):
        require(key in alerts or len(alerts) < MAX_ACTIVE, "monitoring_alert_limit")
        alerts[key] = {"key": key, "severity": severity, "code": code,
                       "value": value, "threshold": threshold}

    def group(key, source):
        status = _status(source)
        if status == "ok":
            observed.add(key)
        else:
            add(key, "unknown", "required_partial" if status == "partial" else "required_unavailable")

    for key, source in (("host.memory", _mapping(host.get("memory"))),
                        ("database.connections", _mapping(database.get("connections"))),
                        ("database.jobs", _mapping(database.get("jobs"))),
                        ("database.ai", _mapping(database.get("ai"))),
                        ("database.uploads", _mapping(database.get("uploads"))),
                        ("database.usage", _mapping(database.get("usage")))):
        group(key, source)

    disks = _mapping(host.get("disks"))
    # A disks-wide unavailable result is distinct from an alias map. Empty maps
    # do not establish that any configured mount was successfully observed.
    if not disks or isinstance(disks.get("status"), str) or len(disks) > MAX_ACTIVE:
        add("host.disks", "unknown", "required_unavailable")
    else:
        valid_disks = True
        for alias, source in disks.items():
            if not isinstance(alias, str) or not re.fullmatch(_ALIAS, alias):
                valid_disks = False
                continue
            group(f"host.disks.{alias}", _mapping(source))
        if valid_disks:
            observed.add("host.disks")
        else:
            add("host.disks", "unknown", "required_unavailable")

    containers = _mapping(host.get("containers"))
    services = _mapping(containers.get("services"))
    known_service_statuses = {"ok", "missing", "stopped", "unhealthy", "ambiguous"}
    # Some collectors use partial to summarize known service failures. Those
    # explicit states retain their own severity; a genuinely missing/unknown
    # service observation still makes the container group unknown.
    understood_partial = (_status(containers) == "partial" and bool(services)
                          and len(services) <= MAX_ACTIVE
                          and all(isinstance(alias, str) and re.fullmatch(_ALIAS, alias)
                                  for alias in services)
                          and all(_status(_mapping(item)) in known_service_statuses
                                  for item in services.values()))
    if understood_partial:
        observed.add("host.containers")
    else:
        group("host.containers", containers)
    if not services:
        observed.discard("host.containers")
        add("host.containers", "unknown", "required_unavailable")
    if len(services) > MAX_ACTIVE:
        observed.discard("host.containers")
        add("host.containers", "unknown", "required_unavailable")
        services = {}
    for alias, source in services.items():
        if not isinstance(alias, str) or not re.fullmatch(_ALIAS, alias):
            observed.discard("host.containers")
            add("host.containers", "unknown", "required_unavailable")
            continue
        key, status = f"host.containers.services.{alias}", _status(_mapping(source))
        if _status(containers) not in {"ok", "partial"}:
            add(key, "unknown", "service_unavailable")
        elif status == "ok":
            observed.add(key)
        elif status in {"missing", "stopped", "unhealthy"}:
            add(key, "critical", f"service_{status}")
        elif status == "ambiguous":
            add(key, "warning", "service_ambiguous")
        else:
            add(key, "unknown", "service_unavailable")

    for key in sorted(_OPTIONAL):
        source, field = _source(metrics, key)
        if (_status(source) not in {"ok", "partial"} or source.get(f"{field}_status") != "ok"
                or not _number(source.get(field))):
            optional.append(key)

    for rule in rules:
        key = rule["metric"]
        if key in optional:
            continue
        source, field = _source(metrics, key)
        value, status = source.get(field), _status(source)
        if key in _QUEUE_AGES and field in source and value is None and status == "ok":
            count = source.get(_QUEUE_AGES[key])
            if _number(count) and count == 0:
                # An observed empty queue has no oldest item and therefore no
                # age. The zero count proves no backlog without inventing an age.
                observed.add(key)
                continue
        if not _number(value) or status not in {"ok", "partial"}:
            add(key, "unknown", "metric_unavailable")
            continue
        if status == "ok":
            observed.add(key)
        if value >= rule["critical"]:
            add(key, "critical", "metric_critical", value, rule["critical"])
        elif value >= rule["warning"]:
            add(key, "warning", "metric_warning", value, rule["warning"])
        elif status == "partial":
            add(key, "unknown", "metric_unavailable")

    usage = _mapping(database.get("usage"))
    for field, code in (("missing_usage_runs", "usage_missing"),
                        ("incomplete_usage_runs", "usage_incomplete")):
        key, value = f"database.usage.{field}", usage.get(field)
        if not _number(value) or _status(usage) not in {"ok", "partial"}:
            add(key, "unknown", "metric_unavailable")
        elif value > 0:
            # This is an explicit completeness condition, not a guessed numeric
            # threshold. Respect a configured critical rule for the same key.
            if key not in alerts or alerts[key]["severity"] != "critical":
                add(key, "warning", code, value)
        elif _status(usage) == "ok":
            observed.add(key)

    severities = {item["severity"] for item in alerts.values()}
    status = next((item for item in ("critical", "unknown", "warning") if item in severities), "ok")
    return {"status": status, "alerts": [alerts[key] for key in sorted(alerts)],
            "observed_keys": sorted(observed), "optional_unavailable": optional}


def _validate_alert(alert, code):
    require(isinstance(alert, dict) and set(alert) == _ALERT_FIELDS and _event_key(alert["key"])
            and isinstance(alert["severity"], str) and alert["severity"] in _SEVERITIES
            and isinstance(alert["code"], str) and alert["code"] in _CODES
            and (alert["value"] is None or _number(alert["value"]))
            and (alert["threshold"] is None or _number(alert["threshold"])), code)


def _validate_event(event, active=False):
    code = "monitoring_alert_state_invalid"
    require(isinstance(event, dict) and set(event) == _EVENT_FIELDS, code)
    _validate_alert({field: event[field] for field in _ALERT_FIELDS}, code)
    require(isinstance(event["id"], str) and bool(re.fullmatch(r"[0-9a-f]{32}", event["id"]))
            and isinstance(event["state"], str)
            and event["state"] in ({"opened", "updated"} if active else {"opened", "updated", "resolved"}), code)
    require(timestamp(event["first_seen"]) <= timestamp(event["last_seen"]), code)


def transition(previous_active, previous_history, evaluation, observed_at):
    """Deduplicate, update severity, and record only proven recoveries locally."""
    observed_time = timestamp(observed_at)
    code = "monitoring_alert_state_invalid"
    require(isinstance(previous_active, dict) and len(previous_active) <= MAX_ACTIVE
            and isinstance(previous_history, list) and len(previous_history) <= MAX_HISTORY, code)
    for key, event in previous_active.items():
        _validate_event(event, active=True)
        require(key == event["key"] and timestamp(event["last_seen"]) <= observed_time, code)
    for event in previous_history:
        _validate_event(event)
    require(isinstance(evaluation, dict) and isinstance(evaluation.get("status"), str)
            and evaluation["status"] in {"ok", *_SEVERITIES}
            and isinstance(evaluation.get("alerts"), list) and len(evaluation["alerts"]) <= MAX_ACTIVE, code)
    current = {}
    for alert in evaluation["alerts"]:
        _validate_alert(alert, code)
        require(alert["key"] not in current, code)
        current[alert["key"]] = alert
    observed = evaluation.get("observed_keys", [])
    require(isinstance(observed, list) and len(observed) <= MAX_ACTIVE * 4
            and all(_event_key(key) for key in observed), code)
    observed = set(observed)
    active = {key: dict(event) for key, event in previous_active.items()}
    history = [dict(event) for event in previous_history]

    for key, alert in current.items():
        old = active.get(key)
        if old is None:
            event = {**alert, "id": uuid.uuid4().hex, "state": "opened",
                     "first_seen": observed_at, "last_seen": observed_at}
            active[key] = event
            history.append(dict(event))
            continue
        weaker = _INCIDENT_LEVEL[alert["severity"]] < _INCIDENT_LEVEL[old["severity"]]
        if weaker and (alert["severity"] == "unknown" or key not in observed):
            # Missing or partial observation cannot prove an incident eased.
            # Known higher values can still prove an upgrade independently.
            active[key] = {**old, "last_seen": observed_at}
            continue
        changed = old["severity"] != alert["severity"]
        event = {**old, **alert, "last_seen": observed_at}
        if changed:
            event["state"] = "updated"
            history.append(dict(event))
        active[key] = event

    for key in set(active) - set(current):
        if key in observed:
            history.append({**active.pop(key), "state": "resolved", "last_seen": observed_at})
    require(len(active) <= MAX_ACTIVE, "monitoring_alert_limit")
    return {"active": active, "history": history[-MAX_HISTORY:]}
