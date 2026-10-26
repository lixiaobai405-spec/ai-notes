"""Fast, daemon-free tests of the configuration and cleanup safety boundary."""

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_https_isolated.py"
SPEC = importlib.util.spec_from_file_location("isolated_https_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def production():
    return {"apps": {"http": {"servers": {"srv0": {
        "listen": [":443"], "read_timeout": 987,
        "tls_connection_policies": [{"default_sni": verifier.SITE_IP}],
        "routes": [{"match": [{"host": [verifier.SITE_IP]}], "handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": "api:8000"}],
             "transport": {"protocol": "http", "response_header_timeout": 654}}]}],
    }}}, "tls": {"automation": {"policies": [{"subjects": [verifier.SITE_IP], "issuers": [{
        "module": "acme", "ca": verifier.ACME_DIRECTORY, "profile": "shortlived",
        "challenges": {"tls-alpn": {"disabled": True}},
    }]}]}}}}


class ConfigurationTests(unittest.TestCase):
    def test_only_certificate_provisioning_changes_and_input_is_immutable(self):
        source = production()
        snapshot = copy.deepcopy(source)
        runtime = verifier.offline_runtime_config(source)
        self.assertEqual(source, snapshot)
        server = runtime["apps"]["http"]["servers"]["srv0"]
        self.assertEqual(server.pop("automatic_https"), {"disable_certificates": True})
        self.assertEqual(runtime["apps"]["http"], source["apps"]["http"])
        self.assertNotIn("automation", runtime["apps"]["tls"])
        self.assertEqual(set(runtime["apps"]["tls"]), {"certificates"})

    def test_preserves_existing_redirect_and_timeout_preferences(self):
        source = production()
        source["apps"]["http"]["servers"]["srv0"]["automatic_https"] = {"disable_redirects": True}
        runtime = verifier.offline_runtime_config(source)
        server = runtime["apps"]["http"]["servers"]["srv0"]
        self.assertEqual(server["automatic_https"], {"disable_redirects": True, "disable_certificates": True})
        self.assertEqual(server["read_timeout"], 987)

    def test_accepts_caddy_app_and_connection_policy_sni_only_when_both_match(self):
        source = production()
        source["apps"]["http"]["default_sni"] = verifier.SITE_IP
        verifier.offline_runtime_config(source)
        source["apps"]["http"]["default_sni"] = "invalid"
        with self.assertRaises(verifier.VerificationError):
            verifier.offline_runtime_config(source)

    def test_rejects_alternative_issuers_profile_challenge_and_sni(self):
        for mutation in ("ca", "profile", "module", "challenge", "sni", "extra_issuer", "static"):
            with self.subTest(mutation=mutation):
                source = production()
                tls = source["apps"]["tls"]
                issuer = tls["automation"]["policies"][0]["issuers"][0]
                if mutation in {"ca", "profile", "module"}:
                    issuer[mutation] = "invalid"
                elif mutation == "challenge":
                    issuer["challenges"]["tls-alpn"]["disabled"] = False
                elif mutation == "sni":
                    source["apps"]["http"]["servers"]["srv0"]["tls_connection_policies"][0]["default_sni"] = "invalid"
                elif mutation == "extra_issuer":
                    tls["automation"]["policies"][0]["issuers"].append({"module": "internal"})
                else:
                    tls["certificates"] = {"load_files": [{"certificate": "/foreign.crt"}]}
                with self.assertRaises(verifier.VerificationError):
                    verifier.offline_runtime_config(source)


class CleanupTests(unittest.TestCase):
    def test_cleanup_never_removes_resource_with_changed_label(self):
        runner = verifier.IsolatedDocker("frontend:local", "python:local")
        runner.resource_creation_started = True
        calls = []
        def docker(args, code, timeout=35):
            calls.append(args)
            return "resource-id" if args[1] == "ls" else ""
        runner.docker = docker
        runner.json = lambda *args, **kwargs: [{"Config": {"Labels": {verifier.LABEL: "foreign"}},
                                              "Labels": {verifier.LABEL: "foreign"}}]
        self.assertFalse(runner.cleanup())
        self.assertFalse(any("rm" in command for command in calls))
        self.assertTrue(all("label=" + runner.label in command for command in calls))

    def test_remote_daemon_is_refused_before_image_or_container_commands(self):
        runner = verifier.IsolatedDocker("frontend:local", "python:local")
        runner.env = {"DOCKER_HOST": "ssh://example.invalid"}
        runner.docker = lambda *args, **kwargs: self.fail("remote Docker command attempted")
        with self.assertRaisesRegex(verifier.VerificationError, "fixture_remote_docker_refused"):
            runner.preflight()
        self.assertTrue(runner.cleanup())

    def test_remote_context_overrides_local_host_and_is_refused_before_daemon(self):
        runner = verifier.IsolatedDocker("frontend:local", "python:local")
        runner.env = {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "remote"}
        runner.docker = lambda *args, **kwargs: self.fail("remote daemon command attempted")
        runner.json = lambda args, code: [{"Endpoints": {"docker": {"Host": "ssh://example.invalid"}}}]
        with self.assertRaisesRegex(verifier.VerificationError, "fixture_remote_docker_refused"):
            runner.preflight()
        self.assertTrue(runner.cleanup())

    def test_verified_endpoint_is_explicit_on_every_daemon_command(self):
        runner = verifier.IsolatedDocker("frontend:local", "python:local")
        runner.executable = "docker"
        runner.endpoint = "unix:///var/run/docker.sock"
        with patch.object(verifier.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "linux\n"
            runner.docker(["info"], "fixture_test")
        self.assertEqual(run.call_args.args[0], ["docker", "--host", runner.endpoint, "info"])

    def test_images_cannot_create_unlabelled_anonymous_volumes(self):
        for foreign in ("frontend:local", "python:local"):
            with self.subTest(foreign=foreign):
                runner = verifier.IsolatedDocker("frontend:local", "python:local")
                runner.env = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
                calls = []
                def docker(args, code, timeout=35):
                    calls.append(args)
                    return "linux\n"
                runner.docker = docker
                runner.json = lambda args, code: {"id": "sha256:" + "a" * 64,
                                                  "volumes": {"/foreign": {}} if args[-1] == foreign else None}
                with self.assertRaisesRegex(verifier.VerificationError, "fixture_image_unmanaged_volume"):
                    runner.preflight()
                self.assertFalse(runner.resource_creation_started)
                self.assertTrue(runner.cleanup())
                self.assertFalse(any("create" in command for command in calls))

    def test_verified_images_are_pinned_to_ids_for_later_create(self):
        runner = verifier.IsolatedDocker("frontend:local", "python:local")
        runner.env = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
        runner.docker = lambda *args, **kwargs: "linux\n"
        runner.json = lambda args, code: {
            "id": "sha256:" + ("a" if args[-1] == "frontend:local" else "b") * 64,
            "volumes": {"/data": {}, "/config": {}} if args[-1] == "frontend:local" else None,
        }
        identities = runner.preflight()
        self.assertEqual(runner.frontend_image, identities["frontend"])
        self.assertEqual(runner.python_image, identities["python"])
        self.assertNotIn("DOCKER_HOST", runner.env)
        self.assertEqual(runner.endpoint, "unix:///var/run/docker.sock")


if __name__ == "__main__":
    unittest.main()
