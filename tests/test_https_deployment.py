"""Parse deployment files with fake inputs; never contact a daemon or read real env files."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
DATABASE_IMAGE = "registry.invalid/ai-notes/postgres@sha256:" + "b" * 64
FIXTURE_VALUES = {
    "ACR_REGISTRY": "registry.invalid",
    "ACR_NAMESPACE": "ai-notes-fixture",
    "RELEASE_TAG": "sha-" + "a" * 40 + "-1-1",
    "POSTGRES_USER": "fixture_user",
    "POSTGRES_PASSWORD": "fixture-database-password",
    "POSTGRES_DB": "fixture_database",
    "DATABASE_URL": "postgresql+psycopg://fixture_user:fixture-database-password@db/fixture_database",
    "APP_ENV": "development",
    "APP_ORIGIN": "http://127.0.0.1:8080",
    "COOKIE_SECURE": "false",
    "OSS_ACCESS_KEY_ID": "fixture-oss-key-id",
    "OSS_ACCESS_KEY_SECRET": "fixture-oss-key-secret",
    "OSS_SECURITY_TOKEN": "fixture-oss-token",
    "DEEPSEEK_API_KEY": "fixture-model-key",
    "SITE_IP": "203.0.113.10",
    "ACME_DIRECTORY": "https://acme.invalid/directory",
    "POSTGRES_IMAGE": DATABASE_IMAGE,
}


@unittest.skipUnless(DOCKER, "Docker Compose client is required; no daemon is used")
class HttpsDeploymentTests(unittest.TestCase):
    def setUp(self):
        # A project directory outside the repository prevents env_file from resolving
        # to a real .env.server. Explicit --env-file also avoids implicit .env reads.
        self.directory = tempfile.TemporaryDirectory(prefix="ai-notes-https-config-")
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name).resolve()
        self.assertFalse(self.project.is_relative_to(ROOT))
        self.env_file = self.project / ".env.server"
        self.write_environment()
        # Keep only OS process-launch settings. No inherited application or
        # COMPOSE_* variable can override fixture values or project/volume names.
        process_names = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)",
        }
        self.process_environment = {
            key: value for key, value in os.environ.items() if key.upper() in process_names
        }

    def write_environment(self, values=None):
        values = FIXTURE_VALUES if values is None else values
        self.env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
        )

    def parse(self, *, https=True, server_file=None, expect_success=True):
        command = [
            DOCKER, "compose", "--project-directory", str(self.project),
            "--env-file", str(self.env_file), "-f", str(server_file or ROOT / "compose.server.yaml"),
        ]
        if https:
            command.extend(["-f", str(ROOT / "compose.https.yaml")])
        command.extend(["--profile", "tools", "config", "--format", "json"])
        result = subprocess.run(
            command, cwd=self.project, env=self.process_environment,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        # Never echo a rendered config or command output into test failures.
        if expect_success:
            self.assertEqual(result.returncode, 0, "Compose rejected isolated deployment fixtures")
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, "Compose accepted missing HTTPS requirements")
        return result

    def test_existing_http_configuration_remains_private_and_unchanged(self):
        config = self.parse(https=False)
        self.assertEqual(config["name"], "ai-notes-server")
        web = config["services"]["web"]
        self.assertNotIn("environment", web)
        self.assertNotIn("volumes", web)
        self.assertEqual(len(web["ports"]), 1)
        port = web["ports"][0]
        self.assertEqual((port["host_ip"], str(port["published"]), port["target"]),
                         ("127.0.0.1", "8080", 80))
        self.assertEqual(config["volumes"]["pgdata"]["name"], "ai-notes-server_pgdata")

    def test_https_replaces_ports_including_the_controlled_server_difference(self):
        server_text = (ROOT / "compose.server.yaml").read_text(encoding="utf-8")
        self.assertEqual(server_text.count('"127.0.0.1:8080:80"'), 1)
        server_copy = self.project / "compose.server.public.yaml"
        server_copy.write_text(
            server_text.replace('"127.0.0.1:8080:80"', '"0.0.0.0:8080:80"'), encoding="utf-8"
        )
        for server_file in (ROOT / "compose.server.yaml", server_copy):
            with self.subTest(server_file=server_file.name):
                config = self.parse(server_file=server_file)
                ports = config["services"]["web"]["ports"]
                self.assertEqual(
                    {(str(port["published"]), port["target"], port["protocol"]) for port in ports},
                    {("80", 80, "tcp"), ("443", 443, "tcp")},
                )
                self.assertEqual(len(ports), 2)
                self.assertTrue(all(port.get("host_ip") in (None, "", "0.0.0.0") for port in ports))
                for name, service in config["services"].items():
                    if name != "web":
                        self.assertFalse(service.get("ports"), f"Unexpected public ports on {name}")

    def test_database_digest_project_and_pgdata_identity_are_preserved(self):
        original = self.parse(https=False)
        secured = self.parse()
        self.assertEqual(secured["name"], original["name"])
        self.assertEqual(secured["volumes"]["pgdata"], original["volumes"]["pgdata"])
        expected_db = dict(original["services"]["db"], image=DATABASE_IMAGE)
        self.assertEqual(secured["services"]["db"], expected_db)
        for name in ("api", "worker", "migrate"):
            self.assertEqual(secured["services"][name], original["services"][name])

    def test_caddy_storage_is_persistent_and_separate_from_pgdata(self):
        config = self.parse()
        for volume in ("caddy_data", "caddy_config"):
            self.assertEqual(config["volumes"][volume]["name"], f"ai-notes-server_{volume}")
        mounts = {mount["target"]: mount for mount in config["services"]["web"]["volumes"]}
        self.assertEqual(set(mounts), {"/etc/caddy/Caddyfile", "/data", "/config"})
        caddyfile = mounts["/etc/caddy/Caddyfile"]
        self.assertEqual(caddyfile["type"], "bind")
        self.assertEqual(Path(caddyfile["source"]), self.project / "deploy/Caddyfile.https")
        self.assertTrue(caddyfile["read_only"])
        for target, source in (("/data", "caddy_data"), ("/config", "caddy_config")):
            self.assertEqual(mounts[target]["type"], "volume")
            self.assertEqual(mounts[target]["source"], source)
            self.assertFalse(mounts[target].get("read_only", False))

    def test_web_receives_only_the_two_public_https_values(self):
        web = self.parse()["services"]["web"]
        self.assertEqual(web["environment"], {
            "SITE_IP": FIXTURE_VALUES["SITE_IP"], "ACME_DIRECTORY": FIXTURE_VALUES["ACME_DIRECTORY"],
        })
        self.assertNotIn("env_file", web)
        for name in ("POSTGRES_PASSWORD", "DATABASE_URL", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
                     "OSS_SECURITY_TOKEN", "DEEPSEEK_API_KEY"):
            self.assertNotIn(FIXTURE_VALUES[name], json.dumps(web))

    def test_missing_or_empty_required_values_are_rejected(self):
        for name in ("SITE_IP", "ACME_DIRECTORY", "POSTGRES_IMAGE"):
            for value in (None, ""):
                with self.subTest(variable=name, value=value):
                    values = dict(FIXTURE_VALUES)
                    if value is None:
                        values.pop(name)
                    else:
                        values[name] = value
                    self.write_environment(values)
                    result = self.parse(expect_success=False)
                    self.assertTrue(name in result.stderr, "Compose did not identify the missing requirement")


if __name__ == "__main__":
    unittest.main()
