"""Exercise release CI evidence locally; no token or remote API is needed."""

import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts import ci_reuse_gate as gate


REPOSITORY = "owner/ai-notes"
REPOSITORY_ID = 1418306558
SHA = "423190a5405b1f5eb4540fdae60fa3295ac53b80"
RUN_ID = 40734911272
WORKFLOW_ID = 383901475
BASE = f"/repos/{REPOSITORY}/actions"
WORKFLOW = BASE + "/workflows/ci.yml"
RUN = BASE + f"/runs/{RUN_ID}"
JOBS = RUN + "/attempts/1/jobs"
TOKEN = "private-token-must-never-appear"


def run(**updates):
    identity = {"id": REPOSITORY_ID, "full_name": REPOSITORY}
    result = {
        "id": RUN_ID, "run_number": 16, "run_attempt": 1,
        "workflow_id": WORKFLOW_ID, "path": ".github/workflows/ci.yml",
        "head_sha": SHA, "head_branch": "main", "event": "push",
        "repository": identity.copy(), "head_repository": identity.copy(),
        "status": "completed", "conclusion": "success",
    }
    result.update(updates)
    return result


def job(**updates):
    result = {
        "id": 101, "name": "verify", "run_id": RUN_ID,
        "run_attempt": 1, "head_sha": SHA,
        "status": "completed", "conclusion": "success",
    }
    result.update(updates)
    return result


def evidence(*, runs=None, jobs=None, detail=None, reread=None):
    runs = [run()] if runs is None else runs
    jobs = [job()] if jobs is None else jobs
    return [
        {"id": WORKFLOW_ID, "path": ".github/workflows/ci.yml", "state": "active"},
        {"total_count": len(runs), "workflow_runs": runs},
        run() if detail is None else detail,
        {"total_count": len(jobs), "jobs": jobs},
        run() if reread is None else reread,
    ]


class FakeAPI:
    def __init__(self, responses):
        self.responses = [item if isinstance(item, Exception) else copy.deepcopy(item) for item in responses]
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        if not self.responses:
            raise AssertionError("Unexpected API request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, gate.ApiPage):
            return response
        return gate.ApiPage(response)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.context = gate.Context(REPOSITORY, REPOSITORY_ID, SHA)

    def evaluate(self, responses):
        api = FakeAPI(responses)
        return gate.evaluate(self.context, api), api

    def assertFallback(self, responses, reason=None):
        decision, api = self.evaluate(responses)
        self.assertFalse(decision.reuse)
        self.assertEqual((decision.run_id, decision.attempt), (0, 0))
        if reason:
            self.assertEqual(decision.reason.value, reason)
        return api

    def test_latest_successful_trusted_push_is_reusable(self):
        decision, api = self.evaluate(evidence())
        self.assertTrue(decision.reuse)
        self.assertEqual((decision.run_id, decision.attempt), (RUN_ID, 1))
        self.assertEqual([path for path, _ in api.calls], [WORKFLOW, WORKFLOW + "/runs", RUN, JOBS, RUN])
        filters = api.calls[1][1]
        self.assertEqual({key: filters[key] for key in ("branch", "event", "head_sha")},
                         {"branch": "main", "event": "push", "head_sha": SHA})
        self.assertNotIn("status", filters)

    def test_latest_failure_or_in_progress_never_uses_older_success(self):
        for status, conclusion in (("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "neutral"), ("completed", "skipped"),
                                   ("in_progress", None), ("queued", None)):
            with self.subTest(status=status, conclusion=conclusion):
                newer = run(id=RUN_ID + 1, run_number=17, status=status, conclusion=conclusion)
                api = self.assertFallback(evidence(runs=[run(), newer]), "latest_not_successful")
                self.assertEqual(len(api.calls), 2)

    def test_run_number_determines_latest_instead_of_list_order_or_id(self):
        old = run(id=RUN_ID + 999, run_number=15, conclusion="failure")
        decision, _ = self.evaluate(evidence(runs=[run(), old]))
        self.assertTrue(decision.reuse)

    def test_workflow_metadata_must_be_active_and_exact(self):
        for updates in ({"id": False}, {"id": "383901475"}, {"path": "other.yml"},
                        {"state": "disabled_manually"}, {"state": None}):
            with self.subTest(updates=updates):
                responses = evidence()
                responses[0].update(updates)
                self.assertFallback(responses)

    def test_all_run_identity_fields_are_required_on_list_detail_and_reread(self):
        changes = [
            {"workflow_id": WORKFLOW_ID + 1}, {"path": "other.yml"},
            {"head_sha": "a" * 40}, {"head_branch": "feature"}, {"event": "pull_request"},
            {"repository": {"id": REPOSITORY_ID + 1, "full_name": REPOSITORY}},
            {"repository": {"id": REPOSITORY_ID, "full_name": "other/ai-notes"}},
            {"head_repository": {"id": REPOSITORY_ID + 1, "full_name": REPOSITORY}},
            {"head_repository": {"id": REPOSITORY_ID, "full_name": "other/ai-notes"}},
            {"run_attempt": 0}, {"run_number": True}, {"id": "40734911272"},
        ]
        for index in (1, 2, 4):
            for changeset in changes:
                with self.subTest(index=index, changes=changeset):
                    responses = evidence()
                    target = responses[index]["workflow_runs"][0] if index == 1 else responses[index]
                    target.update(changeset)
                    self.assertFallback(responses)

    def test_missing_empty_malformed_and_duplicate_run_evidence(self):
        for body in (None, [], {}, {"total_count": 0, "workflow_runs": []},
                     {"total_count": 1, "workflow_runs": []},
                     {"total_count": 0, "workflow_runs": [run()]},
                     {"total_count": True, "workflow_runs": [run()]},
                     {"total_count": 1, "workflow_runs": [None]},
                     {"total_count": 2, "workflow_runs": [run(), run()]},
                     {"total_count": 501, "workflow_runs": []}):
            with self.subTest(body=body):
                responses = evidence()
                responses[1] = body
                self.assertFallback(responses)

    def test_jobs_must_include_one_exact_successful_verify_and_no_unsuccessful_jobs(self):
        cases = [[], [job(name="verify-extra")], [job(), job(id=102)],
                 [job(), job(id=101, name="other")], [None]]
        for status, conclusion in (("completed", "skipped"), ("completed", "neutral"),
                                   ("completed", "failure"), ("completed", "cancelled"),
                                   ("in_progress", None)):
            cases.extend(([job(status=status, conclusion=conclusion)],
                          [job(), job(id=102, name="other", status=status, conclusion=conclusion)]))
        for jobs in cases:
            with self.subTest(jobs=jobs):
                self.assertFallback(evidence(jobs=jobs))
        decision, _ = self.evaluate(evidence(jobs=[job(), job(id=102, name="extra")]))
        self.assertTrue(decision.reuse)

    def test_every_job_must_match_run_attempt_and_sha(self):
        for update in ({"run_id": RUN_ID + 1}, {"run_attempt": 2}, {"head_sha": "a" * 40},
                       {"run_attempt": True}, {"id": False}):
            with self.subTest(update=update):
                self.assertFallback(evidence(jobs=[job(), job(**dict(id=102, name="extra") | update)]))

    def test_incomplete_and_malformed_jobs_are_rejected(self):
        for body in ({}, {"total_count": 2, "jobs": [job()]},
                     {"total_count": 1, "jobs": "bad"},
                     {"total_count": -1, "jobs": []}):
            with self.subTest(body=body):
                responses = evidence()
                responses[3] = body
                self.assertFallback(responses)

    def test_reread_rejects_retry_and_status_races(self):
        for updates in ({"run_attempt": 2}, {"run_number": 17},
                        {"status": "in_progress", "conclusion": None}, {"conclusion": "failure"}):
            with self.subTest(updates=updates):
                self.assertFallback(evidence(reread=run(**updates)))

    def test_latest_retry_attempt_is_used(self):
        responses = evidence(runs=[run(run_attempt=2)], detail=run(run_attempt=2),
                             jobs=[job(run_attempt=2)], reread=run(run_attempt=2))
        decision, api = self.evaluate(responses)
        self.assertTrue(decision.reuse)
        self.assertEqual(decision.attempt, 2)
        self.assertEqual(api.calls[3][0], RUN + "/attempts/2/jobs")

    def test_api_failure_on_any_evidence_request_falls_back(self):
        for index in range(5):
            with self.subTest(index=index):
                responses = evidence()
                responses[index] = gate.EvidenceError(gate.Reason.API_UNAVAILABLE)
                self.assertFallback(responses, "api_unavailable")

    def test_bounded_pagination_must_be_complete_and_stable(self):
        old = [run(id=1000 + index, run_number=index + 1) for index in range(100)]
        latest = run(run_number=101)
        responses = evidence(detail=latest, reread=latest)
        responses[1:2] = [
            gate.ApiPage({"total_count": 101, "workflow_runs": old}, has_next=True),
            {"total_count": 101, "workflow_runs": [latest]},
        ]
        decision, api = self.evaluate(responses)
        self.assertTrue(decision.reuse)
        self.assertEqual(api.calls[2][1]["page"], 2)
        for changed in ({"total_count": 102, "workflow_runs": [latest]},
                        {"total_count": 101, "workflow_runs": []},
                        gate.ApiPage({"total_count": 101, "workflow_runs": [latest]}, has_next=True)):
            with self.subTest(changed=changed):
                bad = copy.deepcopy(responses)
                bad[2] = changed
                self.assertFallback(bad)

    def test_job_pagination_is_attempt_specific_and_complete(self):
        jobs = [job()] + [job(id=102 + index, name="extra") for index in range(100)]
        responses = evidence()
        responses[3:4] = [
            gate.ApiPage({"total_count": 101, "jobs": jobs[:100]}, has_next=True),
            {"total_count": 101, "jobs": jobs[100:]},
        ]
        decision, api = self.evaluate(responses)
        self.assertTrue(decision.reuse)
        self.assertEqual(api.calls[4], (JOBS, {"per_page": 100, "page": 2}))
        responses[4] = {"total_count": 101, "jobs": []}
        self.assertFallback(responses)

    def test_malformed_response_at_any_stage_cannot_be_reused(self):
        for index in range(5):
            for value in (None, [], "untrusted"):
                with self.subTest(index=index, value=value):
                    responses = evidence()
                    responses[index] = value
                    self.assertFallback(responses)

    def test_changed_detail_and_unsuccessful_detail_are_rejected(self):
        for change in ({"id": RUN_ID + 1}, {"run_number": 17}, {"run_attempt": 2},
                       {"status": "in_progress"}, {"conclusion": "failure"}):
            with self.subTest(change=change):
                self.assertFallback(evidence(detail=run(**change)))


class CLITests(unittest.TestCase):
    def invoke(self, mode="lookup", responses=None, extra=(), env=None):
        arguments = [mode, "--repository", REPOSITORY, "--repository-id", str(REPOSITORY_ID), "--sha", SHA]
        if mode == "require":
            arguments += ["--run-id", str(RUN_ID), "--attempt", "1"]
        arguments += list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        api = FakeAPI(evidence() if responses is None else responses)
        environment = {"GH_TOKEN": TOKEN}
        environment.update(env or {})
        with patch.dict(os.environ, environment, clear=True), patch.object(gate, "GitHubAPI", return_value=api), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gate.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue(), api

    def test_lookup_writes_normalized_outputs_and_safe_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory) / "output", Path(directory) / "summary"
            code, stdout, stderr, _ = self.invoke(env={"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)})
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(output.read_text(), f"reuse=true\nrun_id={RUN_ID}\nattempt=1\n")
            self.assertTrue(json.loads(stdout)["reuse"])
            text = summary.read_text(encoding="utf-8")
            self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/1", text)
            self.assertIn(SHA, text)
            self.assertNotIn(TOKEN, text + stdout + stderr)

    def test_lookup_fallback_is_success_with_zero_ids(self):
        responses = evidence(runs=[])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            code, stdout, _, _ = self.invoke(responses=responses, env={"GITHUB_OUTPUT": str(output)})
            self.assertEqual(code, 0)
            body = json.loads(stdout)
            self.assertEqual((body["reuse"], body["run_id"], body["attempt"]), (False, 0, 0))
            self.assertEqual(output.read_text(), "reuse=false\nrun_id=0\nattempt=0\n")

    def test_require_accepts_only_the_same_latest_successful_run_and_attempt(self):
        self.assertEqual(self.invoke(mode="require")[0], 0)
        for responses in (evidence(runs=[]), evidence(runs=[run(conclusion="failure")]),
                          evidence(reread=run(run_attempt=2)),
                          [gate.EvidenceError(gate.Reason.API_UNAVAILABLE)]):
            with self.subTest(responses=responses):
                self.assertNotEqual(self.invoke(mode="require", responses=responses)[0], 0)
        for flag, value in (("--run-id", str(RUN_ID + 1)), ("--attempt", "2")):
            code, stdout, _, _ = self.invoke(mode="require", extra=(flag, value))
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout)["reason"], "expected_evidence_changed")

    def test_require_rejects_a_newer_success_or_retry_even_when_still_green(self):
        newer = run(id=RUN_ID + 1, run_number=17)
        cases = [evidence(runs=[run(), newer], detail=newer,
                          jobs=[job(run_id=RUN_ID + 1)], reread=newer),
                 evidence(runs=[run(run_attempt=2)], detail=run(run_attempt=2),
                          jobs=[job(run_attempt=2)], reread=run(run_attempt=2))]
        for responses in cases:
            with self.subTest(responses=responses):
                code, stdout, _, _ = self.invoke(mode="require", responses=responses)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout)["reason"], "expected_evidence_changed")

    def test_output_write_failure_is_fatal_with_a_fixed_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, stderr, _ = self.invoke(env={"GITHUB_OUTPUT": directory})
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "ci_reuse_gate: output unavailable\n")
            self.assertNotIn(directory, stderr)

    def test_bad_cli_context_is_fatal_and_never_calls_api_or_echoes_values(self):
        for flag, value in (("--repository", "bad\n" + TOKEN), ("--repository", "../repo"),
                            ("--repository-id", "0"), ("--repository-id", "true"),
                            ("--repository-id", "9223372036854775808"),
                            ("--sha", SHA.upper()), ("--sha", "short")):
            with self.subTest(flag=flag, value=value):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(gate, "GitHubAPI") as factory, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        gate.main(["lookup", "--repository", REPOSITORY, "--repository-id", str(REPOSITORY_ID), "--sha", SHA, flag, value])
                self.assertEqual(raised.exception.code, 2)
                factory.assert_not_called()
                self.assertNotIn(value, stderr.getvalue())

    def test_require_missing_or_invalid_expected_identity_is_fatal(self):
        for extra in ([], ["--run-id", "0", "--attempt", "1"], ["--run-id", "1", "--attempt", "false"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    gate.main(["require", "--repository", REPOSITORY, "--repository-id", str(REPOSITORY_ID), "--sha", SHA] + extra)
                self.assertEqual(raised.exception.code, 2)

    def test_missing_token_is_fallback_for_lookup_and_fatal_for_require(self):
        for mode, expected in (("lookup", 0), ("require", 1)):
            code, stdout, _, api = self.invoke(mode=mode, env={"GH_TOKEN": ""})
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(stdout)["reason"], "token_unavailable")
            self.assertFalse(api.calls)

    def test_untrusted_api_strings_and_urls_never_reach_outputs_or_summary(self):
        marker = "UNTRUSTED\nreuse=true\n" + TOKEN
        responses = evidence()
        responses[0]["path"] = marker
        responses[0]["html_url"] = "https://evil.example/" + TOKEN
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary"
            code, stdout, stderr, _ = self.invoke(responses=responses, env={"GITHUB_STEP_SUMMARY": str(summary)})
            self.assertEqual(code, 0)
            combined = stdout + stderr + summary.read_text(encoding="utf-8")
            self.assertNotIn("UNTRUSTED", combined)
            self.assertNotIn(TOKEN, combined)
            self.assertNotIn("evil.example", combined)


class FakeResponse:
    status = 200

    def __init__(self, body=b"{}", headers=None, url=None):
        self.body = io.BytesIO(body)
        self.headers = headers or {}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read1(self, count):
        return self.body.read(count)


class HTTPBoundaryTests(unittest.TestCase):
    def client(self, response=None, error=None):
        opener = unittest.mock.Mock()
        opener.open.side_effect = error
        if response:
            opener.open.return_value = response
        with patch.object(gate.urllib.request, "build_opener", return_value=opener):
            client = gate.GitHubAPI(TOKEN)
        return client, opener

    def test_only_fixed_https_get_with_bounded_timeout_and_env_token_header(self):
        expected_url = "https://api.github.com" + WORKFLOW
        client, opener = self.client(FakeResponse(url=expected_url))
        page = client.get_json(WORKFLOW)
        self.assertEqual(page.data, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, expected_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], gate.REQUEST_TIMEOUT)

    def test_absolute_cross_host_and_malformed_paths_are_rejected_before_http(self):
        for path in ("https://evil.example", "http://api.github.com/", "//evil.example/", "/repos/a/../b", "/repos/a?x=1", "/repos/a\nheader"):
            with self.subTest(path=path):
                client, opener = self.client()
                with self.assertRaises(gate.EvidenceError):
                    client.get_json(path)
                opener.open.assert_not_called()

    def test_redirect_handler_does_not_construct_a_forwarded_request(self):
        handler = gate.NoRedirect()
        with self.assertRaises(gate.EvidenceError):
            handler.redirect_request(None, None, 302, "redirect", {}, "https://evil.example")

    def test_http_errors_timeouts_and_redirected_response_are_sanitized(self):
        errors = [TimeoutError(TOKEN), urllib.error.URLError(TOKEN), http.client.IncompleteRead(b"{}", 10),
                  urllib.error.HTTPError("https://api.github.com", 403, TOKEN, {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client, _ = self.client(error=error)
                with self.assertRaises(gate.EvidenceError) as raised:
                    client.get_json(WORKFLOW)
                self.assertNotIn(TOKEN, str(raised.exception))
        client, _ = self.client(FakeResponse(url="https://evil.example"))
        with self.assertRaises(gate.EvidenceError):
            client.get_json(WORKFLOW)

    def test_response_body_size_json_and_pagination_headers_are_checked(self):
        url = "https://api.github.com" + WORKFLOW
        responses = [FakeResponse(b"not json", url=url), FakeResponse(b"\xff", url=url),
                     FakeResponse(b"{}", headers={"Content-Length": str(gate.MAX_RESPONSE_BYTES + 1)}, url=url),
                     FakeResponse(b"x" * (gate.MAX_RESPONSE_BYTES + 1), url=url)]
        for response in responses:
            with self.subTest(response=response):
                client, _ = self.client(response)
                with self.assertRaises(gate.EvidenceError):
                    client.get_json(WORKFLOW)
        client, _ = self.client(FakeResponse(headers={"Link": '<https://api.github.com/anything>; rel="next"'}, url=url))
        self.assertTrue(client.get_json(WORKFLOW).has_next)

    def test_truncated_body_is_rejected_even_if_partial_bytes_are_valid_json(self):
        client, _ = self.client(FakeResponse(b"{}", headers={"Content-Length": "10"},
                                           url="https://api.github.com" + WORKFLOW))
        with self.assertRaises(gate.EvidenceError):
            client.get_json(WORKFLOW)

    def test_total_deadline_prevents_any_further_request(self):
        with patch.object(gate.time, "monotonic", return_value=0):
            client, opener = self.client()
        with patch.object(gate.time, "monotonic", return_value=gate.TOTAL_TIMEOUT + 1):
            with self.assertRaises(gate.EvidenceError):
                client.get_json(WORKFLOW)
        opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
