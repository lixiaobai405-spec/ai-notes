"""Check workflow trust, image reuse and release contracts without a remote build."""

import ast
import copy
from pathlib import Path
import re
import shlex
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowLoader(yaml.SafeLoader):
    """Keep GitHub's `on` key while retaining true/false boolean inputs."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[first] = [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


def load_yaml(path):
    return yaml.load((ROOT / path).read_text(encoding="utf-8"), Loader=WorkflowLoader)


def evaluate_expression(expression, *, ref, event):
    """Evaluate the workflow's actual boolean/string expression, never Python eval."""
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("github.ref", repr(ref))
    expression = expression.replace("github.event_name", repr(event))
    tree = ast.parse(expression.replace("&&", " and ").replace("||", " or "), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool)):
            return node.value
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            for value in node.values[1:]:
                if isinstance(node.op, ast.And) and not result:
                    return result
                if isinstance(node.op, ast.Or) and result:
                    return result
                result = visit(value)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            return visit(node.left) == visit(node.comparators[0])
        raise AssertionError(f"Unsupported workflow expression node: {ast.dump(node)}")

    return visit(tree.body)


def cache_parameters(value):
    return dict(part.strip().split("=", 1) for part in value.split(","))


def action_steps(job, action):
    return [step for step in job["steps"] if step.get("uses", "").split("@")[0] == action]


def commands(step):
    return [
        shlex.split(command)
        for command in re.split(r"\s*&&\s*|\n", step.get("run", ""))
        if command.strip()
    ]


class BuildCacheWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci = load_yaml(".github/workflows/ci.yml")
        cls.publish = load_yaml(".github/workflows/publish-acr.yml")

    def find_command(self, job, prefix):
        matches = [
            (index, step, command)
            for index, step in enumerate(job["steps"])
            for command in commands(step)
            if command[:len(prefix)] == prefix
        ]
        self.assertEqual(len(matches), 1, f"Expected one command starting with {prefix}")
        return matches[0]

    def test_both_jobs_use_container_builders_and_load_one_image_per_context(self):
        for job in (self.ci["jobs"]["verify"], self.publish["jobs"]["publish"]):
            with self.subTest(job=job.get("runs-on")):
                setup, = action_steps(job, "docker/setup-buildx-action")
                self.assertEqual(setup["uses"].split("@")[1], "v4")
                self.assertEqual(setup["with"]["driver"], "docker-container")
                builds = action_steps(job, "docker/build-push-action")
                self.assertEqual(len(builds), 2)
                self.assertEqual({build["with"]["context"] for build in builds}, {"./backend", "./frontend"})
                for build in builds:
                    self.assertLess(job["steps"].index(setup), job["steps"].index(build))
                    self.assertEqual(build["uses"].split("@")[1], "v7")
                    self.assertEqual(build["with"]["platforms"], "linux/amd64")
                    self.assertIs(build["with"]["push"], False)
                    self.assertIs(build["with"]["load"], True)
                    self.assertNotIn("outputs", build["with"])
                    self.assertNotIn("no-cache", build["with"])

    def test_ci_only_exports_cache_from_trusted_main_events(self):
        cases = [
            ("refs/heads/main", "push", True),
            ("refs/heads/main", "workflow_dispatch", True),
            ("refs/heads/feature", "push", False),
            ("refs/heads/feature", "workflow_dispatch", False),
            ("refs/tags/main", "push", False),
            ("refs/pull/12/merge", "pull_request", False),
            ("refs/heads/main", "pull_request", False),
            ("refs/heads/main", "pull_request_target", False),
            ("refs/heads/main", "issue_comment", False),
            ("refs/heads/main", "workflow_call", False),
        ]
        for build in action_steps(self.ci["jobs"]["verify"], "docker/build-push-action"):
            imported = cache_parameters(build["with"]["cache-from"])
            for ref, event, may_write in cases:
                with self.subTest(context=build["with"]["context"], ref=ref, event=event):
                    exported = evaluate_expression(build["with"]["cache-to"], ref=ref, event=event)
                    if not may_write:
                        self.assertEqual(exported, "")
                    else:
                        parameters = cache_parameters(exported)
                        self.assertEqual(parameters["mode"], "max")
                        self.assertEqual({key: value for key, value in parameters.items() if key != "mode"}, imported)

    def test_ci_and_publish_share_stable_separate_api_v2_cache_scopes(self):
        scopes = {}
        for job in (self.ci["jobs"]["verify"], self.publish["jobs"]["publish"]):
            for build in action_steps(job, "docker/build-push-action"):
                context = build["with"]["context"]
                imported = cache_parameters(build["with"]["cache-from"])
                self.assertEqual(imported["type"], "gha")
                self.assertEqual(imported["version"], "2")
                self.assertNotIn("${{", imported["scope"])
                self.assertEqual(imported["scope"], f"ai-notes-{Path(context).name}-linux-amd64")
                if context in scopes:
                    self.assertEqual(imported["scope"], scopes[context])
                    exported = cache_parameters(build["with"]["cache-to"])
                    self.assertEqual(exported, dict(imported, mode="max"))
                scopes[context] = imported["scope"]
        self.assertEqual(len(set(scopes.values())), 2)

    def test_all_existing_ci_verification_stages_remain_mandatory(self):
        job = self.ci["jobs"]["verify"]
        expected = [
            ("backend", ["alembic", "upgrade", "head"]),
            ("backend", ["alembic", "downgrade", "c2e41a9f3b67"]),
            ("backend", ["alembic", "check"]),
            ("backend", ["pytest", "-q"]),
            ("backend", ["PYTHONPATH=.", "python", "scripts/export_openapi.py"]),
            ("frontend", ["npm", "ci"]),
            ("frontend", ["npm", "run", "check:types"]),
            ("frontend", ["npm", "run", "build"]),
            (None, ["node", "--experimental-strip-types", "--test", "frontend/tests/*.test.mjs"]),
            (None, ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]),
            (None, ["git", "diff", "--exit-code", "--", "backend/openapi.json"]),
            (None, ["docker", "compose", "config", "--quiet"]),
            (None, ["python", "-m", "unittest", "discover", "-s", "tests/https_fixtures", "-p", "test_*.py"]),
        ]
        # Upgrade must still bracket the downgrade, not merely exist somewhere.
        migration = next(step for step in job["steps"] if any(command[:2] == ["alembic", "downgrade"] for command in commands(step)))
        self.assertEqual(commands(migration)[:4], [
            ["alembic", "upgrade", "head"], ["alembic", "downgrade", "c2e41a9f3b67"],
            ["alembic", "upgrade", "head"], ["alembic", "check"],
        ])
        for directory, command in expected:
            with self.subTest(command=command):
                matches = [step for step in job["steps"] if command in commands(step)]
                self.assertTrue(matches)
                for step in matches:
                    self.assertEqual(step.get("working-directory"), directory)
                    self.assertNotIn("if", step)
                    self.assertFalse(step.get("continue-on-error", False))
        self.assertEqual(job["services"]["postgres"]["image"], "postgres:16.8")
        self.assertEqual(migration["env"]["SITE_BACKUP_TEST_CONTAINER"], "${{ job.services.postgres.id }}")

    def test_https_verifier_consumes_both_loaded_images_without_rebuilding(self):
        job = self.ci["jobs"]["verify"]
        index, step, command = self.find_command(job, ["python", "scripts/verify_https_isolated.py"])
        options = dict(zip(command[2::2], command[3::2]))
        self.assertEqual(set(options), {"--frontend-image", "--python-image"})
        loaded = {build["with"]["tags"] for build in action_steps(job, "docker/build-push-action")}
        self.assertEqual({image if ":" in image else image + ":latest" for image in options.values()}, loaded)
        for build in action_steps(job, "docker/build-push-action"):
            self.assertLess(job["steps"].index(build), index)
            self.assertNotIn("if", build)
        self.assertNotIn("if", step)
        all_commands = [command for step in job["steps"] for command in commands(step)]
        self.assertFalse(any(command[:2] == ["docker", "build"] or command[:3] == ["docker", "compose", "build"] for command in all_commands))
        compose = load_yaml("compose.yaml")
        self.assertEqual({service["build"] for service in compose["services"].values() if "build" in service}, {"./backend", "./frontend"})
        self.assertNotIn("build", compose["services"]["db"])

    def test_dependency_caches_follow_existing_lockfiles(self):
        job = self.ci["jobs"]["verify"]
        python, = action_steps(job, "actions/setup-python")
        node, = action_steps(job, "actions/setup-node")
        self.assertEqual(python["uses"], "actions/setup-python@v5")
        self.assertEqual(python["with"]["cache"], "pip")
        python_locks = set(python["with"]["cache-dependency-path"].splitlines())
        self.assertEqual(python_locks, {"backend/requirements.lock", "backend/requirements-dev.lock"})
        self.assertTrue(all((ROOT / lockfile).is_file() for lockfile in python_locks))
        self.assertEqual((node["with"]["cache"], node["with"]["cache-dependency-path"]), ("npm", "frontend/package-lock.json"))

    def test_publish_retains_main_only_reusable_verification_gate(self):
        self.assertEqual(set(self.publish["on"]), {"workflow_dispatch"})
        self.assertTrue({"push", "pull_request", "workflow_call"}.issubset(self.ci["on"]))
        self.assertNotIn("pull_request_target", self.ci["on"])
        verify = self.publish["jobs"]["verify"]
        self.assertEqual(verify["uses"], "./.github/workflows/ci.yml")
        self.assertNotIn("secrets", verify)
        for ref in ("refs/heads/main", "refs/heads/feature", "refs/tags/main", "refs/pull/12/merge"):
            self.assertEqual(bool(evaluate_expression(verify["if"], ref=ref, event="workflow_dispatch")), ref == "refs/heads/main")
        publish = self.publish["jobs"]["publish"]
        self.assertEqual(publish["needs"], "verify")
        self.assertNotIn("if", publish)  # The default success() must not bypass verify.
        self.assertEqual(publish["runs-on"], "ubuntu-24.04")
        self.assertFalse(self.publish["concurrency"]["cancel-in-progress"])
        for workflow in (self.ci, self.publish):
            self.assertEqual(workflow["permissions"], {"contents": "read"})
            self.assertNotIn("secrets.", str(workflow.get("env", {})))
            for job in workflow["jobs"].values():
                for checkout in action_steps(job, "actions/checkout") if "steps" in job else []:
                    self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_registry_secrets_remain_scoped_to_login_after_builds(self):
        job = self.publish["jobs"]["publish"]
        secret_steps = [step for step in job["steps"] if "secrets." in str(step)]
        login, = secret_steps
        self.assertEqual(login["env"], {
            "ACR_USERNAME": "${{ secrets.ACR_USERNAME }}", "ACR_PASSWORD": "${{ secrets.ACR_PASSWORD }}",
        })
        login_index = job["steps"].index(login)
        self.assertIn("--password-stdin", login["run"])
        for build in action_steps(job, "docker/build-push-action"):
            self.assertLess(job["steps"].index(build), login_index)
            for name in ("secrets", "secret-envs", "secret-files", "build-args", "ssh"):
                self.assertNotIn(name, build["with"])
        self.assertNotIn("secrets.", str(job.get("env", {})))
        self.assertNotIn("secrets.", str(self.ci))

    def test_publish_keeps_commit_labels_unique_tag_and_all_three_digests(self):
        job = self.publish["jobs"]["publish"]
        release_steps = [step for step in job["steps"] if 'RELEASE_TAG=sha-' in step.get("run", "")]
        release, = release_steps
        printf, = [shlex.split(line) for line in release["run"].splitlines() if line.strip().startswith("printf ")]
        self.assertEqual(printf[1].removesuffix(r"\n"), "RELEASE_TAG=sha-%s-%s-%s")
        self.assertEqual(printf[2:5], ["$GITHUB_SHA", "$GITHUB_RUN_ID", "$GITHUB_RUN_ATTEMPT"])
        self.assertEqual(printf[-1], "$GITHUB_ENV")
        for build in action_steps(job, "docker/build-push-action"):
            self.assertLess(job["steps"].index(release), job["steps"].index(build))
            component = Path(build["with"]["context"]).name
            self.assertEqual(build["with"]["tags"], "${{ env.ACR_REGISTRY }}/${{ env.ACR_NAMESPACE }}/" + component + ":${{ env.RELEASE_TAG }}")
            labels = dict(line.split("=", 1) for line in build["with"]["labels"].splitlines())
            self.assertEqual(labels["org.opencontainers.image.source"], "https://github.com/${{ github.repository }}")
            self.assertEqual(labels["org.opencontainers.image.revision"], "${{ github.sha }}")
        _, _, pull = self.find_command(job, ["docker", "pull"])
        self.assertEqual(pull[2:], ["--platform", "linux/amd64", "postgres:16.8-alpine"])
        _, _, tag = self.find_command(job, ["docker", "tag"])
        self.assertEqual(tag[2:], ["postgres:16.8-alpine", "$ACR_REGISTRY/$ACR_NAMESPACE/postgres:$RELEASE_TAG"])
        push_index, push, _ = self.find_command(job, ["docker", "push"])
        self.assertEqual(re.findall(r"for image in (.*?); do", push["run"]), ["backend frontend postgres", "backend frontend postgres"])
        self.assertIn(".RepoDigests", push["run"])
        self.assertIn('$ACR_REGISTRY/$ACR_NAMESPACE/$image@', push["run"])
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', push["run"])
        self.assertNotIn("if", push)
        logout = job["steps"][-1]
        self.assertEqual(logout["if"], "always()")
        self.assertIn('docker logout "$ACR_REGISTRY"', logout["run"])
        self.assertLess(push_index, job["steps"].index(logout))


if __name__ == "__main__":
    unittest.main()
