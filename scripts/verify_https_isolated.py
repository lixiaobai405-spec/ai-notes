"""Exercise an already-built frontend in a disposable, offline Docker network.

Only the production Caddyfile and the named fixtures are copied to a temporary
volume. No Compose project, real environment file, database, host port, public
CA, model API, or object store is used. All subprocess failures are redacted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
SITE_IP = "47.111.12.85"
ACME_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
LABEL = "cn.ai-notes.https-isolation"
FIXTURES = ("certificates.py", "mock_upstream.py", "probe.py")
SAFE_CODE = re.compile(r"fixture_[a-z0-9_]{1,90}\Z")


class VerificationError(Exception):
    """An intentionally non-sensitive, stable failure code."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise VerificationError(code)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def check_production_config(config: dict) -> None:
    """Fail closed if adaptation changes the intended public IP/issuer policy."""
    require(isinstance(config, dict), "fixture_adapted_json_invalid")
    issuers = [issuer for item in walk(config) for issuer in item.get("issuers", [])]
    require(len(issuers) == 1, "fixture_production_issuer_count")
    issuer = issuers[0]
    require(issuer.get("module") == "acme" and issuer.get("ca") == ACME_DIRECTORY,
            "fixture_production_issuer_invalid")
    require(issuer.get("profile") == "shortlived", "fixture_production_profile_invalid")
    require(issuer.get("challenges", {}).get("tls-alpn", {}).get("disabled") is True,
            "fixture_production_challenge_invalid")
    require(not any(item.get("module") == "internal" for item in walk(config)),
            "fixture_production_internal_issuer")
    names = [item["default_sni"] for item in walk(config) if "default_sni" in item]
    # Caddy 2.11 emits this at both the HTTP app and TLS connection policy.
    require(bool(names) and all(name == SITE_IP for name in names),
            "fixture_production_default_sni_invalid")
    hosts = [host for item in walk(config) for host in item.get("host", [])
             if isinstance(item.get("host"), list)]
    require(SITE_IP in hosts, "fixture_production_ip_match_missing")
    tls = config.get("apps", {}).get("tls", {})
    require(not tls.get("certificates"), "fixture_production_static_certificate")


def offline_runtime_config(production: dict) -> dict:
    """Replace certificate provisioning, preserving routes, SNI and timeouts.

Automatic HTTPS redirects stay enabled. Certificate automation is removed and
disabled explicitly, so starting this JSON cannot invoke a real ACME issuer.
"""
    check_production_config(production)
    runtime = copy.deepcopy(production)
    tls = runtime["apps"]["tls"]
    tls.pop("automation", None)
    tls["certificates"] = {"load_files": [{
        "certificate": "/data/fixture/server.crt", "key": "/data/fixture/server.key",
    }]}
    servers = runtime["apps"]["http"]["servers"]
    for server in servers.values():
        server.setdefault("automatic_https", {})["disable_certificates"] = True
    # Compare the complete HTTP app, not a hand-picked subset of its routes.
    unchanged = copy.deepcopy(runtime["apps"]["http"])
    for name, server in unchanged["servers"].items():
        original = production["apps"]["http"]["servers"][name]
        if "automatic_https" in original:
            server["automatic_https"] = copy.deepcopy(original["automatic_https"])
        else:
            server.pop("automatic_https", None)
    require(unchanged == production["apps"]["http"], "fixture_http_config_changed")
    require(not any("issuers" in item or "automation" in item for item in walk(runtime)),
            "fixture_runtime_automation_present")
    return runtime


def clean_process_environment() -> dict[str, str]:
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class IsolatedDocker:
    def __init__(self, frontend_image: str, python_image: str):
        self.frontend_image = frontend_image
        self.python_image = python_image
        self.token = uuid.uuid4().hex
        self.prefix = "ai-notes-https-" + self.token[:16]
        self.label = f"{LABEL}={self.token}"
        self.env = clean_process_environment()
        self.endpoint = None
        self.executable = shutil.which("docker")
        self.resources = {"container": [], "network": [], "volume": []}
        self.network = self.prefix + "-net"
        self.volumes = {key: self.prefix + "-" + key for key in ("fixtures", "data", "config")}
        self.stage = "preflight"
        self.resource_creation_started = False

    def docker(self, args: list[str], code: str, timeout: int = 35) -> str:
        require(bool(self.executable), "fixture_docker_unavailable")
        try:
            result = subprocess.run(
                [self.executable, *(["--host", self.endpoint] if self.endpoint else []), *args],
                cwd=tempfile.gettempdir(), env=self.env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise VerificationError(code + "_timeout") from None
        except OSError:
            raise VerificationError(code) from None
        require(result.returncode == 0, code)
        return result.stdout

    def json(self, args: list[str], code: str, timeout: int = 35):
        output = self.docker(args, code, timeout)
        try:
            return json.loads(output)
        except (ValueError, TypeError):
            raise VerificationError(code + "_json") from None

    def preflight(self) -> dict:
        # Refuse SSH/TCP contexts, including a remotely configured Docker CLI.
        context = self.env.get("DOCKER_CONTEXT")
        host = self.env.get("DOCKER_HOST")
        # Docker gives DOCKER_CONTEXT precedence over DOCKER_HOST. Resolve it
        # before any daemon request, then pin every later CLI call to that one
        # verified local endpoint. Context inspection only reads local metadata.
        if context or not host:
            context = context or self.docker(["context", "show"], "fixture_docker_context_failed").strip()
            contexts = self.json(["context", "inspect", context], "fixture_docker_context_failed")
            host = contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
        require(host.startswith(("unix:///", "npipe:////./pipe/")), "fixture_remote_docker_refused")
        self.endpoint = host
        self.env.pop("DOCKER_CONTEXT", None)
        self.env.pop("DOCKER_HOST", None)
        require(self.docker(["info", "--format", "{{.OSType}}"],
                            "fixture_docker_daemon_failed").strip() == "linux",
                "fixture_linux_containers_required")
        images = {}
        for key, value in (("frontend", self.frontend_image), ("python", self.python_image)):
            require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}", value)),
                    "fixture_image_reference_invalid")
            metadata = self.json([
                "image", "inspect", "--format",
                '{"id":{{json .Id}},"volumes":{{json (index .Config "Volumes")}}}', value,
            ], "fixture_cached_image_required")
            identity = metadata.get("id", "")
            require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", identity)),
                    "fixture_image_identity_invalid")
            declared = metadata.get("volumes") or {}
            allowed = {"/data", "/config"} if key == "frontend" else set()
            require(isinstance(declared, dict) and set(declared) <= allowed,
                    "fixture_image_unmanaged_volume")
            images[key] = identity
        # Use the verified immutable IDs for every create; a local tag change
        # cannot swap in another image with unexamined volumes or contents.
        self.frontend_image = images["frontend"]
        self.python_image = images["python"]
        return images

    def create_volume(self, name: str) -> None:
        self.resource_creation_started = True
        self.docker(["volume", "create", "--label", self.label, name], "fixture_volume_create_failed")
        self.resources["volume"].append(name)

    def mounts(self, *, fixtures=True, data=False, config=False, fixture_write=False,
               data_write=False) -> list[str]:
        arguments = []
        for key, enabled, writable in (
            ("fixtures", fixtures, fixture_write), ("data", data, data_write), ("config", config, True),
        ):
            if enabled:
                spec = f"type=volume,source={self.volumes[key]},target=/{key}"
                if not writable:
                    spec += ",readonly"
                arguments += ["--mount", spec]
        return arguments

    def create(self, suffix: str, image: str, command: list[str], *, network="none",
               aliases=(), mounts=(), environment=(), entrypoint="python", user=None) -> str:
        name = self.prefix + "-" + suffix
        args = ["create", "--pull", "never", "--name", name, "--label", self.label,
                "--network", network, "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--env", "DEEPSEEK_API_KEY=", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint", entrypoint]
        if entrypoint == "caddy":
            # The official binary has a file capability; dropping this specific
            # capability prevents exec even before Caddy binds a listener.
            args += ["--cap-add", "NET_BIND_SERVICE"]
        if user:
            args += ["--user", user]
        for alias in aliases:
            args += ["--network-alias", alias]
        for value in environment:
            args += ["--env", value]
        args += [*mounts, image, *command]
        self.docker(args, "fixture_container_create_failed")
        self.resources["container"].append(name)
        return name

    def start_capture(self, name: str, code: str, timeout=45) -> str:
        return self.docker(["start", "--attach", name], code, timeout)

    def helper(self, suffix: str, arguments: list[str], *, data=False, data_write=False,
               fixture_write=False) -> dict:
        name = self.create(suffix, self.python_image, arguments,
                           mounts=self.mounts(data=data, data_write=data_write,
                                              fixture_write=fixture_write), user="0:0")
        output = self.start_capture(name, "fixture_helper_failed", 45)
        try:
            result = json.loads(output)
        except ValueError:
            raise VerificationError("fixture_helper_output_invalid") from None
        require(result.get("status") == "ok", "fixture_helper_failed")
        return result

    def assert_isolation(self) -> None:
        network = self.json(["network", "inspect", self.network], "fixture_network_inspect_failed")[0]
        require(network.get("Internal") is True and network.get("Labels", {}).get(LABEL) == self.token,
                "fixture_network_not_internal")
        for name in self.resources["container"]:
            item = self.json(["container", "inspect", name], "fixture_container_inspect_failed")[0]
            host = item.get("HostConfig", {})
            require(item.get("Config", {}).get("Labels", {}).get(LABEL) == self.token,
                    "fixture_container_label_invalid")
            require(not host.get("PortBindings") and not host.get("Binds") and not host.get("Privileged"),
                    "fixture_host_exposure_detected")
            require(host.get("NetworkMode") in ("none", self.network), "fixture_network_mode_invalid")
            for mount in item.get("Mounts", []):
                require(mount.get("Type") in ("volume", "tmpfs"), "fixture_bind_mount_detected")
                if mount.get("Type") == "volume":
                    require(mount.get("Name") in self.volumes.values(), "fixture_foreign_volume_detected")
            environment = item.get("Config", {}).get("Env", [])
            require("DEEPSEEK_API_KEY=" in environment, "fixture_model_key_not_disabled")

    def cleanup(self) -> bool:
        """Discover and remove only resources bearing this unguessable run label.

        Label discovery also handles a create call whose CLI timed out after the
        daemon created the resource. Never use prune, Compose down or global rm.
        """
        # In particular, never run cleanup commands against a rejected remote
        # context. No mutation has been attempted before preflight succeeds.
        if not self.resource_creation_started:
            return True
        good = True
        kinds = (("container", ["container", "ls", "--all", "--quiet"]),
                 ("network", ["network", "ls", "--quiet"]),
                 ("volume", ["volume", "ls", "--quiet"]))
        for kind, listing in kinds:
            try:
                found = self.docker([*listing, "--filter", "label=" + self.label],
                                    "fixture_cleanup_list_failed", 20).split()
            except VerificationError:
                good = False
                continue
            for identity in found:
                try:
                    # Verify ownership again immediately before every removal.
                    item = self.json([kind, "inspect", identity], "fixture_cleanup_inspect_failed", 20)[0]
                    labels = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
                    require(labels.get(LABEL) == self.token, "fixture_cleanup_label_invalid")
                    command = [kind, "rm", "--force", identity] if kind == "container" else [kind, "rm", identity]
                    self.docker(command, "fixture_cleanup_remove_failed", 25)
                except VerificationError:
                    good = False
        return good

    def execute(self) -> dict:
        started = time.monotonic()
        result = {"status": "failed", "scope": "local_offline_proxy_integration",
                  "site_ip": SITE_IP, "real_acme": False, "real_accounts": False,
                  "model_or_oss_calls": False, "checks": {}}
        try:
            result["images"] = self.preflight()
            for name in self.volumes.values():
                self.create_volume(name)
            with tempfile.TemporaryDirectory(prefix="ai-notes-https-isolation-") as temporary:
                directory = Path(temporary).resolve()
                require(not directory.is_relative_to(ROOT), "fixture_temporary_path_not_isolated")
                for filename in FIXTURES:
                    source = ROOT / "tests" / "https_fixtures" / filename
                    require(source.is_file() and not source.is_symlink(), "fixture_source_invalid")
                    shutil.copyfile(source, directory / filename)
                source = ROOT / "deploy" / "Caddyfile.https"
                require(source.is_file() and not source.is_symlink(), "fixture_caddyfile_invalid")
                caddyfile = source.read_bytes()
                (directory / "Caddyfile.https").write_bytes(caddyfile)
                result["production_caddyfile_sha256"] = hashlib.sha256(caddyfile).hexdigest()
                stager = self.create("stage", self.python_image, ["-c", "pass"],
                                     mounts=self.mounts(fixture_write=True))
                self.docker(["cp", str(directory) + "/.", stager + ":/fixtures/"], "fixture_copy_failed")
                self.stage = "production_adaptation"
                version = self.create("version", self.frontend_image, ["version"],
                                      mounts=self.mounts(data=True, config=True, data_write=True), entrypoint="caddy")
                require(self.start_capture(version, "fixture_caddy_version_failed").strip().startswith("v2.11.7 "),
                        "fixture_caddy_version_mismatch")
                adapter = self.create("adapt", self.frontend_image,
                                      ["adapt", "--config", "/fixtures/Caddyfile.https", "--adapter", "caddyfile", "--validate"],
                                      mounts=self.mounts(data=True, config=True, data_write=True), entrypoint="caddy",
                                      environment=(f"SITE_IP={SITE_IP}", f"ACME_DIRECTORY={ACME_DIRECTORY}"))
                try:
                    production = json.loads(self.start_capture(adapter, "fixture_caddy_adapt_failed"))
                except ValueError:
                    raise VerificationError("fixture_adapted_json_invalid") from None
                runtime = offline_runtime_config(production)
                runtime_file = directory / "runtime.json"
                runtime_file.write_text(json.dumps(runtime), encoding="utf-8")
                self.docker(["cp", str(runtime_file), stager + ":/fixtures/runtime.json"], "fixture_copy_failed")
                result["checks"]["production_adapt_validate_offline"] = "passed"
                result["checks"]["runtime_http_config_unchanged"] = "passed"
                self.stage = "temporary_certificates"
                initial = self.helper("certificates", ["/fixtures/certificates.py", "--marker", self.token],
                                      data=True, data_write=True, fixture_write=True)
                self.docker(["network", "create", "--internal", "--label", self.label, self.network],
                            "fixture_network_create_failed")
                self.resources["network"].append(self.network)
                upstream = self.create("api", self.python_image, ["/fixtures/mock_upstream.py"],
                                       network=self.network, aliases=("api",), mounts=self.mounts())
                self.docker(["start", upstream], "fixture_upstream_start_failed")
                frontend = self.create("web", self.frontend_image,
                                       ["run", "--config", "/fixtures/runtime.json"],
                                       network=self.network, aliases=("frontend",), entrypoint="caddy",
                                       mounts=self.mounts(data=True, config=True, data_write=True))
                self.docker(["start", frontend], "fixture_frontend_start_failed")
                self.stage = "proxy_probe"
                probe = self.create("probe", self.python_image, ["/fixtures/probe.py"],
                                    network=self.network, mounts=self.mounts())
                self.assert_isolation()
                try:
                    probe_result = json.loads(self.start_capture(probe, "fixture_probe_failed", 150))
                except ValueError:
                    raise VerificationError("fixture_probe_output_invalid") from None
                if probe_result.get("status") != "ok":
                    code = probe_result.get("code", "")
                    raise VerificationError(code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "fixture_probe_failed")
                result["probe"] = probe_result
                require(probe_result.get("certificate_sha256") == initial.get("certificate_sha256"),
                        "fixture_certificate_fingerprint_mismatch")
                self.stage = "restart_persistence"
                self.docker(["restart", "--time", "5", frontend], "fixture_restart_failed", 35)
                after = self.helper("state", ["/fixtures/certificates.py", "--inspect"], data=True)
                require(after == initial and after.get("marker") == self.token, "fixture_data_persistence_failed")
                restarted_probe = self.create("restart-probe", self.python_image,
                                              ["/fixtures/probe.py", "--fingerprint-only"],
                                              network=self.network, mounts=self.mounts())
                final = json.loads(self.start_capture(restarted_probe, "fixture_restart_probe_failed", 45))
                require(final.get("status") == "ok" and final.get("certificate_sha256") == initial.get("certificate_sha256"),
                        "fixture_restart_certificate_changed")
                self.assert_isolation()
                result["checks"].update({"internal_network_and_no_host_exposure": "passed",
                                           "certificate_and_data_survive_restart": "passed"})
                result["status"] = "ok"
        except VerificationError as error:
            result["code"] = str(error)
            result["stage"] = self.stage
        except KeyboardInterrupt:
            result["code"] = "fixture_interrupted"
            result["stage"] = self.stage
        except Exception:
            # Do not print Python exception details or captured process output:
            # even an unexpected failure must preserve the redaction boundary.
            result["code"] = "fixture_unexpected_failure"
            result["stage"] = self.stage
        finally:
            cleaned = self.cleanup()
            result["cleanup"] = "passed" if cleaned else "failed"
            if not cleaned:
                result["status"] = "failed"
                result.setdefault("code", "fixture_cleanup_failed")
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-image", required=True, help="Already-built local frontend image; never pulled or built")
    parser.add_argument("--python-image", required=True, help="Already-cached Python image containing openssl; never pulled")
    arguments = parser.parse_args()
    result = IsolatedDocker(arguments.frontend_image, arguments.python_image).execute()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
