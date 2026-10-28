# Deployment Acceleration Implementation Plan

> **For agentic workers:** Use subagent-driven-development for implementation and independent reviews; independent file ownership permits dispatching-parallel-agents. User explicitly authorized both optimizations. Keep the user's main checkout and all earlier changes. The initial implementation phase did not authorize commit or push; the subsequent user request explicitly authorizes committing and pushing only these optimizations to main. Do not deploy, call paid APIs or start production jobs.

**Goal:** Persist front/backend build caches and deliver verifiable Linux offline dependencies for monitoring and backup tools.

**Architecture:** Existing Dockerfiles retain dependency-first layers. Trusted workflows use per-image GHA BuildKit caches; a separate manual wheelhouse workflow exports platform-specific wheels and validates fresh offline installation. Production services and current monitoring remain untouched.

**Tech Stack:** Docker Buildx, official GitHub Actions, Python 3.12 standard library, existing project locks, unittest, local Linux Docker.

## Task 1: Baseline and approved design

- [x] Read current workflows / Dockerfiles / operations boundaries and verify local Linux Docker is available.
- [x] Confirm the two requested optimizations and record design in `docs/superpowers/specs/2026-10-29-deployment-acceleration-design.md`; routine choices stay within user approval.
- [x] Save SHA-256 baseline for all existing modified / untracked files in ignored local operations storage.

## Task 2: Persistent image caches

**Files:** `.github/workflows/ci.yml`, `.github/workflows/publish-acr.yml`, `tests/test_build_cache_workflows.py`.

- [x] Add official Buildx actions and separate frontend/backend linux-amd64 cache scopes using `type=gha` and full intermediate cache export.
- [x] Permit cache export only for trusted main push / main workflow_dispatch; PRs restore only. Preserve main/verify gate, ACR secrets separation, release identity and digest summary.
- [x] Build each validation image once, load locally and reuse in isolated HTTPS checks; preserve every existing application / migration / contract / Compose validation.
- [x] Validate workflow structure and trusted/untrusted event behavior; run appropriate project-isolated tests and workflow lint.

## Task 3: Offline dependency bundles

**Files:** `scripts/ops_wheelhouse.py` (optional focused helper), `ops/wheelhouse/` build context / bootstrap lock, `.github/workflows/build-ops-wheelhouse.yml`, `tests/test_ops_wheelhouse.py`.

- [x] Implement Linux/amd64 CPython3.12 build, cross-platform verification, and fresh-target Linux installation, with monitoring / backup profiles sourced from the project lock.
- [x] Include pinned pip wheel bootstrap, exact version/hash requirements and bounded manifest; reject invalid / missing / extra / tampered / linked inputs before execution, and never replace an existing environment.
- [x] Use a Dockerfile-specific allowlist context; export only wheels and bundle metadata. Provide a main-only manual workflow and scoped dependency / build cache plus verified Artifact upload.
- [x] Ensure fresh installation is strictly offline, handles missing ensurepip, verifies hashes and runs `pip check` / imports, without application configuration or credentials.
- [x] Run negative-path and CLI tests with `backend/.venv/Scripts/python.exe -m unittest discover -s tests -p test_ops_wheelhouse.py -v`.

## Task 4: Actual Linux validation and review

- [x] Build both profiles into ignored local artifact directories; perform verification and fresh install in `--network none` containers without application/DB/OSS mounts. Final Linux builds used verified preseeded wheels after a real METADATA boundary fix; the normal final-source online Docker rebuild remains unclaimed, as recorded in evidence.
- [x] Verify actual dependency-layer reuse in a warm image build and record observed cache behavior; do not claim remote GHA results without running the remote workflow.
- [x] Independent specification review, fixes, then independent code-quality review; integrate and rerun affected checks.
- [x] Write `docs/部署提速与离线依赖.md` and `docs/task-evidence/2026-10-29-deployment-acceleration.md`; add README entry and update this checklist.
- [x] Final diff / sensitive path / baseline checks; preserve all earlier work and leave changes uncommitted under user Git rules.
