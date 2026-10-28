# Publish CI Reuse Implementation Plan

> **For agentic workers:** Use subagent-driven-development for the isolated helper task and independent specification / quality reviews. User approved reuse of successful checks for the same commit. Remain on the existing main checkout and preserve earlier changes. The initial implementation scope did not authorize commit/push; the follow-up request explicitly authorizes committing and pushing only this optimization to main, then checking CI and fixing failures. Do not dispatch ACR release workflows or touch production.

**Goal:** Avoid a second complete CI run for a trusted, already-verified release SHA while preserving explicit fail-closed release authorization.

**Architecture:** A read-only standard-library selector supplies normalized job outputs. Missing/uncertain evidence triggers the existing reusable CI. An explicit two-path condition handles skipped dependencies. Reused evidence is revalidated immediately before ACR login; source checkout stays pinned to event SHA.

**Tech Stack:** GitHub Actions REST API, Python standard library/unittest, existing YAML tests and actionlint.

## Task 1: Baseline and contracts

- [x] Read current CI / release workflow, tests, project instructions and existing changes.
- [x] Inspect official API / skipped dependency docs and real run / attempt-job metadata.
- [x] Record design and exact selector / fallback / publish security conditions.
- [x] Save existing modified / untracked file hashes to ignored operations storage.

## Task 2: Read-only CI evidence helper

**Create:** `scripts/ci_reuse_gate.py`, `tests/test_ci_reuse_gate.py`.

- [x] Write failing tests for matching latest successful trusted push/main SHA and rejected statuses / identities / skipped verify / changed attempts.
- [x] Implement `lookup` (safe fallback) and `require --run-id N --attempt N` (refuse uncertain evidence) CLI. Inputs: `--repository`, `--repository-id`, `--sha`; token only from `GH_TOKEN`, no credential printing. `lookup` outputs `reuse=true|false`, `run_id`, `attempt` via GITHUB_OUTPUT when present; JSON also emitted. Require outputs provenance in GITHUB_STEP_SUMMARY.
- [x] Restrict read-only HTTPS requests to api.github.com with bounded time / size, no redirect token forwarding; validate pagination completeness and latest-attempt job identities, then re-read run metadata.
- [x] Run `backend/.venv/Scripts/python.exe -m unittest discover -s tests -p test_ci_reuse_gate.py -v` and self-review.

## Task 3: Workflow integration

**Modify:** `.github/workflows/publish-acr.yml`, `tests/test_build_cache_workflows.py`.

- [x] Add main/dispatch-only select job with actions:read scoped to selector and publisher, fixed-SHA checkout, normalized outputs and summary.
- [x] Make original reusable CI conditional on explicit false output and successful selector.
- [x] Add explicit publish truth table using always(), !cancelled(), required selector success and exactly one valid reuse/fallback path.
- [x] Pin checkout and assert local HEAD, and add require recheck before ACR credential step only for reuse. Preserve the source SHA/tag/digest/cache contracts and no secrets inheritance into CI.
- [x] Extend actual expression evaluation tests for all dependency result/flag/cancellation cases, run existing cache tests and lint both workflows.

## Task 4: Integrated verification and handoff

**Modify:** `docs/部署提速与离线依赖.md`; **create:** `docs/task-evidence/2026-10-29-publish-ci-reuse.md`.

- [x] Make a read-only live query for the successful 423190a full SHA using a child-process token obtained without printing / persisting it; verify gate evidence points to the real run. Do not dispatch release.
- [x] Run full root tests in the project interpreter and all affected workflow lint; validate wrong SHA / API failure fallback with controlled data.
- [x] Independent spec review, fix gaps, then independent code quality review and affected test reruns.
- [x] Document reuse / fallback / recheck and expected saved CI duration, with local vs remote validation limits.
- [x] Check diff / sensitive content, confirm existing hashes unchanged, update checklist; leave code uncommitted under user Git rules.
