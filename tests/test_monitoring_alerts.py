"""Pure monitoring rules; no database, host commands, files or credentials."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from scripts import monitoring_alerts as alerts
from scripts.monitoring_common import MonitoringError


T0 = "2026-10-26T00:00:00Z"
T1 = "2026-10-26T00:01:00Z"
T2 = "2026-10-26T00:02:00Z"
MEMORY = "host.memory.used_percent"
RULE = {"metric": MEMORY, "warning": 85, "critical": 95}


def healthy():
    return {
        "host": {
            "memory": {"status": "ok", "used_percent": 0},
            "disks": {"data": {"status": "ok", "used_percent": 0}},
            "containers": {"status": "ok", "services": {"api": {"status": "ok"}}},
        },
        "database": {
            "connections": {"status": "ok", "used_percent": 0},
            "jobs": {"status": "ok", "ready_queued": 0, "scheduled_queued": 0, "running": 0,
                     "failed_total": 0, "failed_created_window": 0, "oldest_ready_seconds": 0,
                     "expired_leases": 0},
            "ai": {"status": "ok", "queued": 0, "running": 0, "errors_finished_window": 0,
                   "provider_errors_finished_window": 0, "oldest_queued_seconds": 0, "expired_leases": 0},
            "uploads": {"status": "ok", "created_window": 0, "expired_created_window": 0,
                        "rejected_created_window": 0, "processing_failed_created_window": 0,
                        "pdf_rejected_created_window": 0,
                        "http_failure_rate": None, "http_failure_rate_status": "unavailable"},
            "usage": {"status": "ok", "known_total_tokens": 0, "terminal_runs_window": 0,
                      "missing_usage_runs": 0, "incomplete_usage_runs": 0,
                      "cost": None, "cost_status": "unavailable"},
        },
    }


def evaluation(severity=None, key=MEMORY, value=99):
    if severity is None:
        return {"status": "ok", "alerts": [], "observed_keys": [key]}
    return {"status": severity, "alerts": [{"key": key, "severity": severity,
            "code": "metric_unavailable" if severity == "unknown" else f"metric_{severity}",
            "value": None if severity == "unknown" else value,
            "threshold": None if severity == "unknown" else 95}],
            "observed_keys": [] if severity == "unknown" else [key]}


class MonitoringAssertions:
    def assert_code(self, code, function, *args):
        with self.assertRaises(MonitoringError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)


class EvaluationTests(MonitoringAssertions, unittest.TestCase):
    def test_zero_values_and_unavailable_optional_sources_are_normal(self):
        result = alerts.evaluate(healthy(), [RULE])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["optional_unavailable"],
                         ["database.uploads.http_failure_rate", "database.usage.cost"])
        self.assertIn(MEMORY, result["observed_keys"])

    def test_warning_and_critical_boundaries_are_inclusive(self):
        for value, expected in ((84.9, "ok"), (85, "warning"), (94.9, "warning"), (95, "critical")):
            with self.subTest(value=value):
                metrics = healthy()
                metrics["host"]["memory"]["used_percent"] = value
                result = alerts.evaluate(metrics, [RULE])
                self.assertEqual(result["status"], expected)
                if expected != "ok":
                    self.assertEqual(result["alerts"][0]["value"], value)
                    self.assertEqual(result["alerts"][0]["threshold"], RULE[expected])

    def test_unconfigured_numbers_have_no_guessed_threshold(self):
        metrics = healthy()
        metrics["host"]["memory"]["used_percent"] = 99
        metrics["database"]["jobs"]["running"] = 1_000_000
        self.assertEqual(alerts.evaluate(metrics, [])["status"], "ok")

    def test_numeric_values_reject_bool_string_nonfinite_negative_and_huge_integer(self):
        for value in (True, False, "99", None, [], {}, float("nan"), float("inf"), -1, 10 ** 400):
            with self.subTest(value_type=type(value).__name__):
                metrics = healthy()
                metrics["host"]["memory"]["used_percent"] = value
                result = alerts.evaluate(metrics, [RULE])
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["alerts"][0]["value"])
                self.assertNotIn(MEMORY, result["observed_keys"])

    def test_explicit_zero_warning_and_equal_limits_are_supported(self):
        result = alerts.evaluate(healthy(), [{"metric": MEMORY, "warning": 0, "critical": 0}])
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["alerts"][0]["value"], 0)

    def test_observed_empty_queues_have_no_age_and_do_not_warn(self):
        metrics = healthy()
        metrics["database"]["jobs"]["oldest_ready_seconds"] = None
        metrics["database"]["ai"]["oldest_queued_seconds"] = None
        keys = ("database.jobs.oldest_ready_seconds", "database.ai.oldest_queued_seconds")
        rules = [{"metric": key, "warning": 0, "critical": 0} for key in keys]
        result = alerts.evaluate(metrics, rules)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["alerts"], [])
        self.assertTrue(set(keys) <= set(result["observed_keys"]))
        self.assertIsNone(metrics["database"]["jobs"]["oldest_ready_seconds"])
        self.assertIsNone(metrics["database"]["ai"]["oldest_queued_seconds"])

    def test_missing_queue_age_requires_trusted_explicit_zero_count(self):
        for group, age, count_field in (("jobs", "oldest_ready_seconds", "ready_queued"),
                                         ("ai", "oldest_queued_seconds", "queued")):
            key = f"database.{group}.{age}"
            for count in (1, None, False, "0", -1):
                with self.subTest(group=group, count=count):
                    metrics = healthy()
                    metrics["database"][group].update({age: None, count_field: count})
                    result = alerts.evaluate(metrics, [{"metric": key, "warning": 300, "critical": 900}])
                    self.assertEqual(result["status"], "unknown")
                    self.assertNotIn(key, result["observed_keys"])
            for missing in (age, count_field):
                metrics = healthy()
                metrics["database"][group][age] = None
                metrics["database"][group].pop(missing)
                result = alerts.evaluate(metrics, [{"metric": key, "warning": 300, "critical": 900}])
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn(key, result["observed_keys"])
            metrics = healthy()
            metrics["database"][group].update({age: None, "status": "partial"})
            result = alerts.evaluate(metrics, [{"metric": key, "warning": 300, "critical": 900}])
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn(key, result["observed_keys"])

    def test_partial_values_may_prove_critical_but_cannot_prove_recovery(self):
        metrics = healthy()
        metrics["host"]["memory"].update(status="partial", used_percent=99)
        result = alerts.evaluate(metrics, [RULE])
        self.assertEqual(result["status"], "critical")
        self.assertNotIn(MEMORY, result["observed_keys"])
        metrics["host"]["memory"]["used_percent"] = 10
        result = alerts.evaluate(metrics, [RULE])
        self.assertEqual(result["status"], "unknown")
        self.assertIn(MEMORY, {item["key"] for item in result["alerts"]})

    def test_unavailable_group_cannot_reuse_its_old_numeric_value(self):
        metrics = healthy()
        metrics["host"]["memory"].update(status="unavailable", used_percent=99)
        result = alerts.evaluate(metrics, [RULE])
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(all(item["severity"] == "unknown" for item in result["alerts"]))

    def test_missing_and_malformed_groups_produce_unknown(self):
        for value in ({}, None, "SENSITIVE_SENTINEL", {"host": []}):
            result = alerts.evaluate(value, [RULE])
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))
        for owner, field in (("host", "memory"), ("host", "containers"),
                             ("database", "connections"), ("database", "jobs"),
                             ("database", "ai"), ("database", "uploads"), ("database", "usage")):
            with self.subTest(group=field):
                metrics = healthy()
                metrics[owner][field] = {"status": "unavailable", "code": "SENSITIVE_SENTINEL"}
                result = alerts.evaluate(metrics, [RULE])
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))

    def test_malformed_status_types_become_unknown_without_type_errors(self):
        for status in ([], {}, True):
            metrics = healthy()
            metrics["host"]["memory"]["status"] = status
            metrics["host"]["containers"]["services"]["api"]["status"] = status
            self.assertEqual(alerts.evaluate(metrics, [RULE])["status"], "unknown")

    def test_disk_aliases_are_allowlisted_and_missing_disks_are_unknown(self):
        rule = {"metric": "host.disks.data.used_percent", "warning": 85, "critical": 95}
        metrics = healthy()
        metrics["host"]["disks"]["data"]["used_percent"] = 99
        self.assertEqual(alerts.evaluate(metrics, [rule])["status"], "critical")
        for disks in ({}, {"status": "unavailable"}, {"../SENSITIVE_SENTINEL": {"status": "ok"}}):
            metrics["host"]["disks"] = disks
            result = alerts.evaluate(metrics, [rule])
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))

    def test_service_business_states_are_distinct_from_collection_failure(self):
        expected = {"missing": "critical", "stopped": "critical", "unhealthy": "critical",
                    "ambiguous": "warning", "unavailable": "unknown", "partial": "unknown", "ok": "ok"}
        for group_status in ("ok", "partial"):
            for service_status, severity in expected.items():
                with self.subTest(group=group_status, service=service_status):
                    metrics = healthy()
                    metrics["host"]["containers"].update(status=group_status,
                        services={"api": {"status": service_status}})
                    self.assertEqual(alerts.evaluate(metrics, [])["status"], severity)

    def test_missing_services_are_unknown_and_unsafe_alias_is_not_echoed(self):
        for services in (None, {}, {"../SENSITIVE_SENTINEL": {"status": "missing"}}):
            metrics = healthy()
            metrics["host"]["containers"]["services"] = services
            result = alerts.evaluate(metrics, [])
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("SENSITIVE_SENTINEL", json.dumps(result))

    def test_invalid_or_excess_services_cannot_mark_container_group_observed(self):
        for status in ("ok", "partial"):
            for services in ({"../SENSITIVE_SENTINEL": {"status": "ambiguous"}},
                             {f"s{index}": {"status": "ambiguous"} for index in range(129)}):
                metrics = healthy()
                metrics["host"]["containers"].update(status=status, services=services)
                result = alerts.evaluate(metrics, [])
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("host.containers", result["observed_keys"])

    def test_unavailable_containers_cannot_reuse_stale_child_statuses(self):
        metrics = healthy()
        metrics["host"]["containers"].update(status="unavailable",
                                              services={"api": {"status": "stopped"}})
        result = alerts.evaluate(metrics, [])
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(all(item["severity"] == "unknown" for item in result["alerts"]))
        self.assertNotIn("host.containers.services.api", result["observed_keys"])

    def test_status_is_a_valid_disk_alias_when_its_value_is_an_observation(self):
        metrics = healthy()
        metrics["host"]["disks"] = {"status": {"status": "ok", "used_percent": 0}}
        rule = {"metric": "host.disks.status.used_percent", "warning": 85, "critical": 95}
        self.assertEqual(alerts.evaluate(metrics, [rule])["status"], "ok")

    def test_host_summary_partial_does_not_duplicate_healthy_child_alerts(self):
        metrics = healthy()
        metrics["host"]["status"] = "partial"
        self.assertEqual(alerts.evaluate(metrics, [RULE])["status"], "ok")

    def test_unknown_precedes_warning_and_known_critical_precedes_unknown(self):
        metrics = healthy()
        metrics["database"]["jobs"]["status"] = "unavailable"
        metrics["host"]["memory"]["used_percent"] = 90
        self.assertEqual(alerts.evaluate(metrics, [RULE])["status"], "unknown")
        metrics["host"]["memory"]["used_percent"] = 99
        self.assertEqual(alerts.evaluate(metrics, [RULE])["status"], "critical")

    def test_usage_missing_and_incomplete_are_explicit_without_thresholds(self):
        for field, expected_code in (("missing_usage_runs", "usage_missing"),
                                     ("incomplete_usage_runs", "usage_incomplete")):
            metrics = healthy()
            metrics["database"]["usage"][field] = 1
            result = alerts.evaluate(metrics, [])
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["alerts"][0]["code"], expected_code)
        metrics["database"]["usage"]["status"] = "partial"
        self.assertEqual(alerts.evaluate(metrics, [])["status"], "unknown")

    def test_configured_usage_critical_is_not_downgraded_by_completeness_warning(self):
        metrics = healthy()
        metrics["database"]["usage"]["missing_usage_runs"] = 2
        rule = {"metric": "database.usage.missing_usage_runs", "warning": 1, "critical": 2}
        self.assertEqual(alerts.evaluate(metrics, [rule])["status"], "critical")

    def test_pdf_rejection_metric_can_trigger_even_when_jobs_succeeded(self):
        metrics = healthy()
        metrics["database"]["uploads"]["pdf_rejected_created_window"] = 2
        rule = {"metric": "database.uploads.pdf_rejected_created_window", "warning": 1, "critical": 2}
        result = alerts.evaluate(metrics, [rule])
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["alerts"], [{"key": rule["metric"], "severity": "critical",
                                           "code": "metric_critical", "value": 2, "threshold": 2}])
        self.assertEqual(metrics["database"]["jobs"]["failed_created_window"], 0)

    def test_optional_unavailable_does_not_create_severe_alerts_even_if_configured(self):
        rules = [{"metric": key, "warning": 1, "critical": 2}
                 for key in ("database.uploads.http_failure_rate", "database.usage.cost")]
        result = alerts.evaluate(healthy(), rules)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["alerts"], [])
        metrics = healthy()
        metrics["database"]["usage"].update(cost_status="ok", cost=3)
        self.assertEqual(alerts.evaluate(metrics, rules)["status"], "critical")

    def test_thresholds_only_accept_explicit_finite_nonnegative_numeric_rules(self):
        invalid = (None, {}, [None], [dict(RULE, metric="host.memory.raw")],
                   [dict(RULE, metric="host.disks.../secret.used_percent")],
                   [dict(RULE, metric="database.__dict__.password")],
                   [dict(RULE, metric=["host", "memory"])], [dict(RULE, extra="raw")],
                   [{"metric": MEMORY, "warning": 1}], [RULE, RULE], [RULE] * 129)
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                self.assert_code("monitoring_thresholds_invalid", alerts.validate_thresholds, value)
        for value in (True, "85", None, float("nan"), float("inf"), -1, 10 ** 400):
            self.assert_code("monitoring_thresholds_invalid", alerts.validate_thresholds, [dict(RULE, warning=value)])
            self.assert_code("monitoring_thresholds_invalid", alerts.validate_thresholds, [dict(RULE, critical=value)])
        self.assert_code("monitoring_thresholds_invalid", alerts.validate_thresholds, [dict(RULE, warning=96)])

    def test_approved_aggregate_paths_are_accepted_and_inputs_are_unchanged(self):
        paths = ("host.memory.used_percent", "host.disks.data.used_percent",
                 "database.connections.used_percent", "database.jobs.ready_queued",
                 "database.jobs.scheduled_queued", "database.jobs.failed_created_window",
                 "database.jobs.oldest_ready_seconds", "database.jobs.expired_leases",
                 "database.ai.provider_errors_finished_window", "database.ai.oldest_queued_seconds",
                 "database.ai.expired_leases", "database.uploads.processing_failed_created_window",
                 "database.uploads.pdf_rejected_created_window",
                 "database.usage.known_total_tokens", "database.usage.terminal_runs_window")
        rules = [{"metric": key, "warning": 1, "critical": 2} for key in paths]
        self.assertEqual(alerts.validate_thresholds(rules), rules)
        metrics, expected = healthy(), healthy()
        original_rules = deepcopy(rules)
        alerts.evaluate(metrics, rules)
        self.assertEqual(metrics, expected)
        self.assertEqual(rules, original_rules)


class TransitionTests(MonitoringAssertions, unittest.TestCase):
    def test_opens_once_and_updates_last_seen_without_duplicate_history(self):
        first = alerts.transition({}, [], evaluation("critical"), T0)
        second = alerts.transition(first["active"], first["history"], evaluation("critical", value=100), T1)
        event = second["active"][MEMORY]
        self.assertRegex(event["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(event["id"], first["active"][MEMORY]["id"])
        self.assertEqual(event["first_seen"], T0)
        self.assertEqual(event["last_seen"], T1)
        self.assertEqual(event["value"], 100)
        self.assertEqual(len(second["history"]), 1)

    def test_severity_changes_append_updated_and_preserve_incident_identity(self):
        first = alerts.transition({}, [], evaluation("warning"), T0)
        second = alerts.transition(first["active"], first["history"], evaluation("critical"), T1)
        third = alerts.transition(second["active"], second["history"], evaluation("warning"), T2)
        self.assertEqual([event["state"] for event in third["history"]], ["opened", "updated", "updated"])
        self.assertEqual({event["id"] for event in third["history"]}, {first["active"][MEMORY]["id"]})
        self.assertEqual(third["active"][MEMORY]["first_seen"], T0)

    def test_unknown_or_missing_observation_does_not_resolve_known_incident(self):
        for severity in ("critical", "warning"):
            first = alerts.transition({}, [], evaluation(severity), T0)
            second = alerts.transition(first["active"], first["history"], evaluation("unknown"), T1)
            self.assertEqual(second["active"][MEMORY]["severity"], severity)
            self.assertEqual(second["active"][MEMORY]["last_seen"], T1)
            self.assertEqual(len(second["history"]), 1)
            second = alerts.transition(first["active"], first["history"], {"status": "ok", "alerts": []}, T1)
            self.assertIn(MEMORY, second["active"])

    def test_real_trusted_recovery_records_resolved(self):
        first = alerts.transition({}, [], evaluation("critical"), T0)
        second = alerts.transition(first["active"], first["history"], evaluation(), T1)
        self.assertEqual(second["active"], {})
        self.assertEqual(second["history"][-1]["state"], "resolved")
        self.assertEqual(second["history"][-1]["first_seen"], T0)
        self.assertEqual(second["history"][-1]["last_seen"], T1)

    def test_real_evaluation_missing_or_partial_data_cannot_resolve_then_ok_can(self):
        metrics = healthy()
        metrics["host"]["memory"]["used_percent"] = 99
        first = alerts.transition({}, [], alerts.evaluate(metrics, [RULE]), T0)
        metrics["host"]["memory"].update(status="partial", used_percent=0)
        second = alerts.transition(first["active"], first["history"], alerts.evaluate(metrics, [RULE]), T1)
        self.assertEqual(second["active"][MEMORY]["severity"], "critical")
        metrics["host"]["memory"]["status"] = "ok"
        third = alerts.transition(second["active"], second["history"], alerts.evaluate(metrics, [RULE]), T2)
        self.assertEqual(third["active"], {})
        self.assertEqual(third["history"][-1]["state"], "resolved")

    def test_partial_lower_usage_cannot_downgrade_before_trusted_recovery(self):
        metrics = healthy()
        key = "database.usage.known_total_tokens"
        rule = {"metric": key, "warning": 85, "critical": 95}
        metrics["database"]["usage"]["known_total_tokens"] = 100
        first = alerts.transition({}, [], alerts.evaluate(metrics, [rule]), T0)
        metrics["database"]["usage"].update(status="partial", known_total_tokens=90,
                                               missing_usage_runs=1, incomplete_usage_runs=1)
        partial = alerts.evaluate(metrics, [rule])
        self.assertNotIn(key, partial["observed_keys"])
        self.assertEqual(next(item["severity"] for item in partial["alerts"] if item["key"] == key), "warning")
        second = alerts.transition(first["active"], first["history"], partial, T1)
        self.assertEqual(second["active"][key]["severity"], "critical")
        self.assertEqual(second["active"][key]["value"], 100)
        self.assertEqual(second["active"][key]["last_seen"], T1)
        self.assertEqual([event["state"] for event in second["history"] if event["key"] == key], ["opened"])
        metrics["database"]["usage"].update(status="ok", known_total_tokens=80,
                                               missing_usage_runs=0, incomplete_usage_runs=0)
        third = alerts.transition(second["active"], second["history"], alerts.evaluate(metrics, [rule]), T2)
        self.assertEqual(third["active"], {})
        self.assertEqual([event["state"] for event in third["history"] if event["key"] == key], ["opened", "resolved"])

    def test_partial_known_higher_usage_can_still_upgrade_incident(self):
        metrics = healthy()
        key = "database.usage.known_total_tokens"
        rule = {"metric": key, "warning": 85, "critical": 95}
        metrics["database"]["usage"]["known_total_tokens"] = 90
        first = alerts.transition({}, [], alerts.evaluate(metrics, [rule]), T0)
        metrics["database"]["usage"].update(status="partial", known_total_tokens=100,
                                               missing_usage_runs=1)
        partial = alerts.evaluate(metrics, [rule])
        self.assertNotIn(key, partial["observed_keys"])
        second = alerts.transition(first["active"], first["history"], partial, T1)
        self.assertEqual(second["active"][key]["severity"], "critical")
        self.assertEqual([event["state"] for event in second["history"] if event["key"] == key], ["opened", "updated"])

    def test_unknown_new_incident_can_resolve_when_its_group_is_observed(self):
        first = alerts.transition({}, [], evaluation("unknown", key="database.jobs"), T0)
        second = alerts.transition(first["active"], first["history"], evaluation(key="database.jobs"), T1)
        self.assertEqual(second["active"], {})

    def test_container_unknown_resolves_to_known_partial_ambiguous_warning(self):
        metrics = healthy()
        metrics["host"]["containers"].update(status="unavailable",
                                              services={"api": {"status": "unavailable"}})
        first = alerts.transition({}, [], alerts.evaluate(metrics, []), T0)
        self.assertEqual(first["active"]["host.containers"]["severity"], "unknown")
        metrics["host"]["containers"].update(status="partial",
                                              services={"api": {"status": "ambiguous"}})
        current = alerts.evaluate(metrics, [])
        self.assertEqual(current["status"], "warning")
        self.assertIn("host.containers", current["observed_keys"])
        second = alerts.transition(first["active"], first["history"], current, T1)
        self.assertNotIn("host.containers", second["active"])
        self.assertEqual({event["severity"] for event in second["active"].values()}, {"warning"})
        group_history = [event for event in second["history"] if event["key"] == "host.containers"]
        self.assertEqual(group_history[-1]["state"], "resolved")

    def test_explicit_empty_queue_proves_recovery_without_fabricating_age(self):
        metrics = healthy()
        metrics["database"]["jobs"].update(ready_queued=2, oldest_ready_seconds=1000)
        rule = {"metric": "database.jobs.oldest_ready_seconds", "warning": 300, "critical": 900}
        first = alerts.transition({}, [], alerts.evaluate(metrics, [rule]), T0)
        metrics["database"]["jobs"].update(ready_queued=0, oldest_ready_seconds=None)
        second = alerts.transition(first["active"], first["history"], alerts.evaluate(metrics, [rule]), T1)
        self.assertEqual(second["active"], {})
        self.assertEqual(second["history"][-1]["state"], "resolved")
        self.assertIsNone(metrics["database"]["jobs"]["oldest_ready_seconds"])

    def test_history_is_bounded_after_repeated_open_and_recovery(self):
        state = {"active": {}, "history": []}
        moment = datetime(2026, 10, 26, tzinfo=timezone.utc)
        for index in range(110):
            current = evaluation("warning") if index % 2 == 0 else evaluation()
            state = alerts.transition(state["active"], state["history"], current,
                                      (moment + timedelta(seconds=index)).isoformat())
        self.assertEqual(state["active"], {})
        self.assertEqual(len(state["history"]), 100)
        self.assertEqual(state["history"][0]["first_seen"], (moment + timedelta(seconds=10)).isoformat())

    def test_active_limit_is_explicit_and_never_silently_drops_old_incidents(self):
        current = {"status": "critical", "alerts": [evaluation("critical", key=f"host.containers.services.s{i}")["alerts"][0]
                                                     for i in range(128)]}
        first = alerts.transition({}, [], current, T0)
        self.assertEqual(len(first["active"]), 128)
        self.assertEqual(len(first["history"]), 100)
        self.assert_code("monitoring_alert_limit", alerts.transition, first["active"], first["history"],
                         evaluation("critical"), T1)
        self.assertEqual(len(first["active"]), 128)

    def test_previous_state_and_evaluation_are_not_mutated(self):
        first = alerts.transition({}, [], evaluation("critical"), T0)
        original, current = deepcopy(first), evaluation()
        expected = deepcopy(current)
        alerts.transition(first["active"], first["history"], current, T1)
        self.assertEqual(first, original)
        self.assertEqual(current, expected)

    def test_private_or_malformed_state_is_rejected_with_fixed_codes(self):
        first = alerts.transition({}, [], evaluation("critical"), T0)
        for field, value in (("code", "SENSITIVE_SENTINEL"), ("severity", []), ("id", "raw-id"),
                             ("value", True), ("state", []), ("key", "private.password")):
            event = {**first["active"][MEMORY], field: value}
            self.assert_code("monitoring_alert_state_invalid", alerts.transition,
                             {MEMORY: event}, [], evaluation(), T1)
        event = {**first["active"][MEMORY], "raw": "SENSITIVE_SENTINEL"}
        self.assert_code("monitoring_alert_state_invalid", alerts.transition, {MEMORY: event}, [], evaluation(), T1)
        self.assert_code("monitoring_alert_state_invalid", alerts.transition, {}, [],
                         {"status": [], "alerts": []}, T1)
        self.assert_code("monitoring_alert_state_invalid", alerts.transition, {}, [],
                         {"status": "ok", "alerts": [], "observed_keys": ["private.password"]}, T1)

    def test_future_active_time_and_duplicate_alerts_are_rejected(self):
        first = alerts.transition({}, [], evaluation("critical"), T2)
        self.assert_code("monitoring_alert_state_invalid", alerts.transition,
                         first["active"], first["history"], evaluation(), T1)
        current = evaluation("critical")
        current["alerts"] *= 2
        self.assert_code("monitoring_alert_state_invalid", alerts.transition, {}, [], current, T1)


if __name__ == "__main__":
    unittest.main()
