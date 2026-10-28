"""Select trusted CI evidence, or require unchanged evidence before release.

Only GitHub REST GET requests are made. API uncertainty is a cache miss for
lookup and a release failure for require; raw API content is never displayed.
"""

import argparse
from dataclasses import dataclass
from enum import Enum
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_ORIGIN = "https://api.github.com"
WORKFLOW_PATH = ".github/workflows/ci.yml"
PAGE_SIZE = 100
MAX_ITEMS = 500
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 10
TOTAL_TIMEOUT = 60
MAX_INTEGER = 2**63 - 1
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class Reason(str, Enum):
    TRUSTED = "trusted_ci"
    NO_RUN = "no_matching_run"
    WORKFLOW_UNTRUSTED = "workflow_untrusted"
    INVALID = "invalid_evidence"
    INCOMPLETE = "incomplete_evidence"
    IDENTITY = "identity_mismatch"
    LATEST_UNSUCCESSFUL = "latest_not_successful"
    JOBS_UNSUCCESSFUL = "jobs_not_successful"
    RUN_CHANGED = "run_changed"
    API_UNAVAILABLE = "api_unavailable"
    TOKEN_UNAVAILABLE = "token_unavailable"
    EXPECTED_CHANGED = "expected_evidence_changed"


class EvidenceError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason.value)


def positive_integer(value):
    return type(value) is int and 0 < value <= MAX_INTEGER


def repository_argument(value):
    if not REPOSITORY_PATTERN.fullmatch(value) or value.split("/")[1] in (".", ".."):
        raise argparse.ArgumentTypeError("invalid repository")
    return value


def integer_argument(value):
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or not positive_integer(int(value)):
        raise argparse.ArgumentTypeError("invalid positive integer")
    return int(value)


def sha_argument(value):
    if not SHA_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid sha")
    return value


@dataclass(frozen=True)
class Context:
    repository: str
    repository_id: int
    sha: str


@dataclass(frozen=True)
class Decision:
    reuse: bool
    run_id: int
    attempt: int
    reason: Reason


@dataclass(frozen=True)
class ApiPage:
    data: object
    has_next: bool = False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never build a second request carrying Authorization, even on this host.
        raise EvidenceError(Reason.API_UNAVAILABLE)


class GitHubAPI:
    def __init__(self, token):
        self.token = token
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.opener = urllib.request.build_opener(NoRedirect())

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceError(Reason.API_UNAVAILABLE)
        return min(REQUEST_TIMEOUT, remaining)

    def get_json(self, path, params=None):
        if (not re.fullmatch(r"/repos/[A-Za-z0-9_./-]+", path)
                or ".." in path.split("/") or "//" in path):
            raise EvidenceError(Reason.API_UNAVAILABLE)
        url = API_ORIGIN + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "ai-notes-ci-reuse-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self.opener.open(request, timeout=self.remaining()) as response:
                if response.status != 200 or response.geturl() != url:
                    raise EvidenceError(Reason.API_UNAVAILABLE)
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                           or int(length) > MAX_RESPONSE_BYTES):
                    raise EvidenceError(Reason.API_UNAVAILABLE)
                chunks, size = [], 0
                while True:
                    self.remaining()
                    # read1 performs at most one underlying read, allowing the
                    # overall deadline to be checked even for a trickling peer.
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise EvidenceError(Reason.API_UNAVAILABLE)
                self.remaining()
                if length is not None and size != int(length):
                    raise EvidenceError(Reason.API_UNAVAILABLE)
                data = json.loads(b"".join(chunks).decode("utf-8"))
                has_next = bool(re.search(r';\s*rel="next"', response.headers.get("Link", "")))
                return ApiPage(data, has_next)
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, RecursionError):
            raise EvidenceError(Reason.API_UNAVAILABLE) from None


def complete_items(api, path, key, filters=None):
    """Use only constructed page URLs, never Link URLs supplied by the API."""
    items, total = [], None
    for page_number in range(1, MAX_ITEMS // PAGE_SIZE + 1):
        params = dict(filters or {}, per_page=PAGE_SIZE, page=page_number)
        page = api.get_json(path, params)
        body = page.data
        if not isinstance(body, dict):
            raise EvidenceError(Reason.INVALID)
        count, batch = body.get("total_count"), body.get(key)
        if (type(count) is not int or not 0 <= count <= MAX_ITEMS
                or not isinstance(batch, list)):
            raise EvidenceError(Reason.INCOMPLETE)
        if total is None:
            total = count
        if count != total or len(batch) != min(PAGE_SIZE, total - len(items)):
            raise EvidenceError(Reason.INCOMPLETE)
        items.extend(batch)
        if len(items) == total:
            if page.has_next:
                raise EvidenceError(Reason.INCOMPLETE)
            return items
    raise EvidenceError(Reason.INCOMPLETE)


def checked_run(body, context, workflow_id):
    if not isinstance(body, dict):
        raise EvidenceError(Reason.INVALID)
    if any(not positive_integer(body.get(key)) for key in ("id", "run_number", "run_attempt")):
        raise EvidenceError(Reason.INVALID)
    if (type(body.get("workflow_id")) is not int or body["workflow_id"] != workflow_id
            or body.get("path") != WORKFLOW_PATH or body.get("head_sha") != context.sha
            or body.get("head_branch") != "main" or body.get("event") != "push"):
        raise EvidenceError(Reason.IDENTITY)
    for key in ("repository", "head_repository"):
        repository = body.get(key)
        if (not isinstance(repository, dict) or type(repository.get("id")) is not int
                or repository["id"] != context.repository_id
                or repository.get("full_name") != context.repository):
            raise EvidenceError(Reason.IDENTITY)
    return body


def successful(body):
    return body.get("status") == "completed" and body.get("conclusion") == "success"


def same_run(first, second):
    return all(first[key] == second[key] for key in ("id", "run_number", "run_attempt"))


def evaluate(context, api):
    try:
        base = f"/repos/{context.repository}/actions"
        workflow_path = base + "/workflows/ci.yml"
        workflow = api.get_json(workflow_path).data
        if (not isinstance(workflow, dict) or not positive_integer(workflow.get("id"))
                or workflow.get("path") != WORKFLOW_PATH or workflow.get("state") != "active"):
            raise EvidenceError(Reason.WORKFLOW_UNTRUSTED)
        workflow_id = workflow["id"]
        runs = complete_items(api, workflow_path + "/runs", "workflow_runs", {
            "branch": "main", "event": "push", "head_sha": context.sha,
        })
        if not runs:
            raise EvidenceError(Reason.NO_RUN)
        for candidate in runs:
            checked_run(candidate, context, workflow_id)
        if (len({item["id"] for item in runs}) != len(runs)
                or len({item["run_number"] for item in runs}) != len(runs)):
            raise EvidenceError(Reason.INVALID)
        latest = max(runs, key=lambda item: item["run_number"])
        if not successful(latest):
            raise EvidenceError(Reason.LATEST_UNSUCCESSFUL)
        run_path = base + f"/runs/{latest['id']}"
        detail = checked_run(api.get_json(run_path).data, context, workflow_id)
        if not same_run(latest, detail):
            raise EvidenceError(Reason.RUN_CHANGED)
        if not successful(detail):
            raise EvidenceError(Reason.LATEST_UNSUCCESSFUL)
        jobs = complete_items(api, run_path + f"/attempts/{detail['run_attempt']}/jobs", "jobs")
        seen, verify_count = set(), 0
        for job in jobs:
            if not isinstance(job, dict) or not positive_integer(job.get("id")) or job["id"] in seen:
                raise EvidenceError(Reason.INVALID)
            seen.add(job["id"])
            if (type(job.get("run_id")) is not int or job["run_id"] != detail["id"]
                    or type(job.get("run_attempt")) is not int or job["run_attempt"] != detail["run_attempt"]
                    or job.get("head_sha") != context.sha):
                raise EvidenceError(Reason.IDENTITY)
            if not successful(job):
                raise EvidenceError(Reason.JOBS_UNSUCCESSFUL)
            verify_count += job.get("name") == "verify"
        if verify_count != 1:
            raise EvidenceError(Reason.JOBS_UNSUCCESSFUL)
        reread = checked_run(api.get_json(run_path).data, context, workflow_id)
        if not same_run(detail, reread) or not successful(reread):
            raise EvidenceError(Reason.RUN_CHANGED)
        return Decision(True, detail["id"], detail["run_attempt"], Reason.TRUSTED)
    except EvidenceError as error:
        return Decision(False, 0, 0, error.reason)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors echo arguments, which can contain secrets.
        self.exit(2, "ci_reuse_gate: invalid arguments\n")


def parser():
    root = SafeParser(description=__doc__)
    modes = root.add_subparsers(dest="mode", required=True)
    for mode in ("lookup", "require"):
        command = modes.add_parser(mode)
        command.add_argument("--repository", required=True, type=repository_argument)
        command.add_argument("--repository-id", required=True, type=integer_argument)
        command.add_argument("--sha", required=True, type=sha_argument)
        if mode == "require":
            command.add_argument("--run-id", required=True, type=integer_argument)
            command.add_argument("--attempt", required=True, type=integer_argument)
    return root


def emit(arguments, decision):
    flag = "true" if decision.reuse else "false"
    url = (f"https://github.com/{arguments.repository}/actions/runs/"
           f"{decision.run_id}/attempts/{decision.attempt}") if decision.reuse else ""
    if arguments.mode == "lookup" and os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"reuse={flag}\nrun_id={decision.run_id}\nattempt={decision.attempt}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(f"\n### CI reuse gate\n\nMode: `{arguments.mode}`  \n"
                          f"Repository: `{arguments.repository}`  \nSHA: `{arguments.sha}`  \n"
                          f"Reuse: `{flag}`  \nReason: `{decision.reason.value}`  \n")
            if url:
                summary.write(f"Evidence: [run {decision.run_id}, attempt {decision.attempt}]({url})\n")
            if arguments.mode == "require":
                summary.write(f"Expected run: `{arguments.run_id}`, attempt: `{arguments.attempt}`\n")
    print(json.dumps({"reuse": decision.reuse, "run_id": decision.run_id,
                      "attempt": decision.attempt, "reason": decision.reason.value,
                      "source_url": url}, separators=(",", ":")))


def main(argv=None):
    arguments = parser().parse_args(argv)
    context = Context(arguments.repository, arguments.repository_id, arguments.sha)
    token = os.environ.get("GH_TOKEN", "")
    if not token or len(token) > 4096 or any(ord(character) < 33 or ord(character) > 126 for character in token):
        decision = Decision(False, 0, 0, Reason.TOKEN_UNAVAILABLE)
    else:
        decision = evaluate(context, GitHubAPI(token))
    if (arguments.mode == "require" and decision.reuse
            and (decision.run_id != arguments.run_id or decision.attempt != arguments.attempt)):
        decision = Decision(False, 0, 0, Reason.EXPECTED_CHANGED)
    try:
        emit(arguments, decision)
    except OSError:
        print("ci_reuse_gate: output unavailable", file=sys.stderr)
        return 1
    return 0 if arguments.mode == "lookup" or decision.reuse else 1


if __name__ == "__main__":
    raise SystemExit(main())
