"""Synthetic wheels and process boundaries only: no cloud, Docker or package downloads."""

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
wh = importlib.import_module("ops_wheelhouse")
BACKEND = (ROOT / "backend/requirements.lock").read_text(encoding="utf-8")


def make_wheel(directory, name, version, *, tag="py3-none-any", metadata_name=None,
               duplicate_metadata=False, metadata_size=None):
    stem = f"{name.replace('-', '_')}-{version}"
    destination = directory / f"{stem}-{tag}.whl"
    with zipfile.ZipFile(destination, "w") as archive:
        metadata = (f"Metadata-Version: 2.1\nName: {metadata_name or name}\n"
                    f"Version: {version}\n")
        if metadata_size is not None:
            metadata += "\n"
            metadata += "x" * (metadata_size - len(metadata.encode("utf-8")))
        archive.writestr(f"{stem}.dist-info/METADATA", metadata)
        archive.writestr(f"{stem}.dist-info/WHEEL",
                         f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n")
        archive.writestr(f"{stem}.dist-info/RECORD", "")
        if duplicate_metadata:
            archive.writestr("other-1.dist-info/METADATA", metadata)
    return destination


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "wheels").mkdir()
        (self.bundle / "install.py").write_bytes((ROOT / "scripts/ops_wheelhouse.py").read_bytes())
        (self.bundle / "backend-requirements.lock").write_text(BACKEND, encoding="utf-8")
        (self.bundle / "bootstrap.lock").write_text("pip==24.0\n", encoding="utf-8")
        self.pins = wh.profile_pins(BACKEND, "monitoring")
        (self.bundle / "requirements.lock").write_text(wh.pins_text(self.pins), encoding="utf-8")
        self.wheels = {name: make_wheel(self.bundle / "wheels", name, version)
                       for name, version in {**self.pins, "pip": "24.0"}.items()}
        self.refresh_requirements()
        self.refresh_manifest()

    def refresh_requirements(self):
        (self.bundle / "requirements-offline.txt").write_text(
            wh.hashed_requirements(self.pins, self.wheels), encoding="utf-8")
        (self.bundle / "bootstrap-offline.txt").write_text(
            wh.hashed_requirements({"pip": "24.0"}, self.wheels), encoding="utf-8")

    def refresh_manifest(self):
        self.manifest = {"schema_version": 1, "profile": "monitoring", "target": wh.TARGET,
                         "backend_lock_sha256": wh.digest(self.bundle / "backend-requirements.lock"),
                         "bootstrap_lock_sha256": wh.digest(self.bundle / "bootstrap.lock"),
                         "files": wh.inventory(self.bundle)}
        self.write_manifest()

    def write_manifest(self):
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def assert_rejected_without_install(self):
        with patch.object(wh, "run_process") as run:
            with self.assertRaises(wh.WheelhouseError):
                wh.install(self.bundle, self.root / "new-venv")
            run.assert_not_called()
        self.assertFalse((self.root / "new-venv").exists())

    def test_verify_is_portable_and_cli_returns_json(self):
        with patch.object(wh, "check_platform", side_effect=AssertionError("must not run")):
            self.assertEqual(wh.verify(self.bundle)["profile"], "monitoring")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(wh.main(["verify", "--bundle", str(self.bundle)]), 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_hash_damage_prevents_every_install_process(self):
        self.wheels["pip"].write_bytes(b"damaged")
        self.assert_rejected_without_install()

    def test_missing_file_prevents_install(self):
        self.wheels["psycopg"].unlink()
        self.assert_rejected_without_install()

    def test_extra_file_and_extra_empty_directory_are_rejected(self):
        extra = self.bundle / "surprise.txt"
        extra.write_text("unexpected")
        self.assert_rejected_without_install()
        extra.unlink()
        (self.bundle / "empty-extra").mkdir()
        self.assert_rejected_without_install()

    def test_extra_wheel_cannot_be_added_even_with_updated_manifest(self):
        make_wheel(self.bundle / "wheels", "surprise", "1.0")
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_bundle_symlink_is_rejected(self):
        link = self.root / "link"
        try:
            link.symlink_to(self.bundle, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc.__class__.__name__}")
        with self.assertRaisesRegex(wh.WheelhouseError, "symlink"):
            wh.verify(link)

    def test_file_symlink_is_rejected(self):
        source = self.wheels["pip"]
        outside = self.root / "outside.whl"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc.__class__.__name__}")
        self.assert_rejected_without_install()

    def test_duplicate_manifest_keys_rejected(self):
        encoded = json.dumps(self.manifest)
        (self.bundle / "manifest.json").write_text('{"profile":"backup",' + encoded[1:],
                                                   encoding="utf-8")
        self.assert_rejected_without_install()

    def test_duplicate_manifest_paths_and_escape_rejected(self):
        self.manifest["files"]["../outside"] = {"size": 1, "sha256": "a" * 64}
        self.write_manifest()
        self.assert_rejected_without_install()

    def test_malformed_and_oversized_metadata_are_rejected(self):
        self.manifest["files"]["install.py"]["size"] = True
        self.write_manifest()
        self.assert_rejected_without_install()
        (self.bundle / "manifest.json").write_bytes(b" " * (wh.MAX_MANIFEST + 1))
        self.assert_rejected_without_install()

    def test_hash_requirements_cannot_point_to_network_or_change_version(self):
        (self.bundle / "requirements-offline.txt").write_text(
            "--index-url https://example.invalid/simple\npsycopg==9.9\n", encoding="utf-8")
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_profile_cannot_expand_monitoring_scope(self):
        (self.bundle / "requirements.lock").write_text(
            wh.pins_text({**self.pins, "fastapi": "0.143.0"}), encoding="utf-8")
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_bootstrap_version_is_fixed(self):
        (self.bundle / "bootstrap.lock").write_text("pip==25.0\n", encoding="utf-8")
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_wrong_target_metadata_is_rejected(self):
        self.manifest["target"] = {**wh.TARGET, "architecture": "aarch64"}
        self.write_manifest()
        self.assert_rejected_without_install()

    def test_wheel_metadata_identity_is_checked_after_hashes(self):
        old = self.wheels["psycopg"]
        old.unlink()
        self.wheels["psycopg"] = make_wheel(old.parent, "psycopg", self.pins["psycopg"],
                                           metadata_name="unrelated")
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_duplicate_wheel_metadata_is_rejected(self):
        old = self.wheels["pip"]
        old.unlink()
        self.wheels["pip"] = make_wheel(old.parent, "pip", "24.0", duplicate_metadata=True)
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_source_and_wrong_platform_wheels_are_rejected(self):
        wheel = self.wheels["pip"]
        wheel.unlink()
        self.wheels["pip"] = make_wheel(wheel.parent, "pip", "24.0", tag="cp312-cp312-win_amd64")
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_unsupported_wheel_glibc_and_python_tags(self):
        for tag in (("cp313", "cp313", "manylinux_2_36_x86_64"),
                    ("cp312", "cp312", "manylinux_2_38_x86_64"),
                    ("cp312", "cp312", "musllinux_1_2_x86_64"),
                    ("cp312", "cp312", "manylinux_2_17_aarch64")):
            self.assertFalse(wh.compatible_tag(*tag))
        self.assertTrue(wh.compatible_tag("cp39", "abi3", "manylinux_2_17_x86_64"))
        self.assertTrue(wh.compatible_tag("py3", "none", "any"))

    def test_wheel_archive_path_escape_is_rejected(self):
        wheel = self.wheels["pip"]
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("../outside.py", "unexpected")
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_wheel_missing_wheel_record_is_rejected(self):
        wheel = self.wheels["pip"]
        stem = "pip-24.0"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{stem}.dist-info/METADATA",
                             "Metadata-Version: 2.1\nName: pip\nVersion: 24.0\n")
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_valid_wheel_metadata_can_exceed_lock_text_limit_up_to_one_mib(self):
        for size in (133006, 1024 * 1024):
            with self.subTest(size=size):
                wheel = self.wheels["pip"]
                wheel.unlink()
                self.wheels["pip"] = make_wheel(wheel.parent, "pip", "24.0", metadata_size=size)
                self.refresh_requirements()
                self.refresh_manifest()
                self.assertEqual(wh.verify(self.bundle)["profile"], "monitoring")

    def test_wheel_metadata_above_one_mib_is_rejected_before_install(self):
        wheel = self.wheels["pip"]
        wheel.unlink()
        self.wheels["pip"] = make_wheel(wheel.parent, "pip", "24.0",
                                        metadata_size=1024 * 1024 + 1)
        self.refresh_requirements()
        self.refresh_manifest()
        self.assert_rejected_without_install()

    def test_install_refuses_existing_empty_environment_and_relative_path(self):
        target = self.root / "existing"
        target.mkdir()
        for selected in (target, Path("relative-venv")):
            with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
                with self.assertRaises(wh.WheelhouseError):
                    wh.install(self.bundle, selected)
                run.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_parent_components_cannot_hide_bundle_environment_overlap(self):
        alias = self.root / "alias"
        alias.mkdir()
        bundle = alias / ".." / self.bundle.name
        target = self.bundle / "new-venv"
        with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
            with self.assertRaisesRegex(wh.WheelhouseError, "venv_overlaps_bundle"):
                wh.install(bundle, target)
            run.assert_not_called()
        self.assertFalse(target.exists())

    def test_verify_accepts_real_parent_components_without_symlinks(self):
        alias = self.root / "alias"
        alias.mkdir()
        bundle = alias / ".." / self.bundle.name
        self.assertEqual(wh.verify(bundle)["profile"], "monitoring")

    def test_bundle_symlink_is_checked_before_parent_normalization(self):
        alias = self.root / "alias"
        real = self.root / "real"
        real.mkdir()
        try:
            alias.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc.__class__.__name__}")
        bundle = alias / ".." / self.bundle.name
        with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
            with self.assertRaisesRegex(wh.WheelhouseError, "symlink"):
                wh.install(bundle, self.root / "new-venv")
            run.assert_not_called()
        self.assertFalse((self.root / "new-venv").exists())

    def test_wrong_host_never_creates_environment(self):
        with patch.object(wh, "check_platform",
                          side_effect=wh.WheelhouseError("wrong_platform")):
            self.assert_rejected_without_install()

    def test_installer_uses_only_verified_wheel_bootstrap_and_new_venv(self):
        target = self.root / "new"
        with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
            result = wh.install(self.bundle, target)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 5)
        self.assertTrue(result["offline"])
        self.assertIn("--copies", commands[0])
        self.assertIn("--without-pip", commands[0])
        self.assertNotIn("ensurepip", " ".join(" ".join(command) for command in commands))
        self.assertIn(str(self.wheels["pip"]), commands[1])
        self.assertIn("runpy.run_module('pip'", commands[1][3])
        for command in commands[1:3]:
            for required in ("--no-index", "--require-hashes", "--no-deps",
                             "--only-binary=:all:", "--no-cache-dir"):
                self.assertIn(required, command)
            self.assertEqual(command[command.index("--find-links") + 1],
                             str(self.bundle / "wheels"))
            self.assertNotIn("--upgrade", command)
            self.assertEqual(command[0], str(target / "bin/python"))
        self.assertEqual(commands[3][-1], "check")
        self.assertNotIn("app.", commands[4][-1])
        self.assertTrue(all(call.kwargs["offline"] for call in run.call_args_list))

    def test_failed_install_retains_new_target_and_never_deletes_or_overwrites(self):
        target = self.root / "new"
        with patch.object(wh, "check_platform"), patch.object(
                wh, "run_process", side_effect=wh.WheelhouseError("simulated_failure")):
            with self.assertRaises(wh.WheelhouseError):
                wh.install(self.bundle, target)
        self.assertTrue(target.is_dir())
        with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
            with self.assertRaisesRegex(wh.WheelhouseError, "already_exists"):
                wh.install(self.bundle, target)
            run.assert_not_called()

    def test_linux_profile_preserves_pins_and_excludes_only_false_conditions(self):
        full = wh.profile_pins(BACKEND, "backup")
        self.assertNotIn("tzdata", full)
        self.assertIn("uvloop", full)
        self.assertIn("pycparser", full)
        self.assertIn("psycopg-binary", full)
        self.assertEqual(set(self.pins), wh.MONITORING)
        self.assertTrue(all(full[name] == version for name, version in self.pins.items()))
        for invalid in ("thing>=1", "--index-url https://example.invalid", "thing @ https://example.invalid/x",
                        "thing==1\nTHING==2", "thing==1 ; unknown_marker == 'x'",
                        "thing==1 ; __import__('os').system('unexpected')"):
            with self.subTest(invalid=invalid), self.assertRaises(wh.WheelhouseError):
                wh.parse_lock(invalid)

    def test_build_binary_only_no_deps_and_does_not_overwrite(self):
        output = self.root / "output"
        backend = self.root / "backend.lock"
        backend.write_text(BACKEND, encoding="utf-8")
        bootstrap = self.root / "bootstrap.lock"
        bootstrap.write_text("pip==24.0\n", encoding="utf-8")

        def download(command, *, offline):
            self.assertFalse(offline)
            self.assertIn("--only-binary=:all:", command)
            self.assertIn("--no-deps", command)
            self.assertEqual(command[command.index("--index-url") + 1], "https://pypi.org/simple")
            for name, version in {**self.pins, "pip": "24.0"}.items():
                make_wheel(output / "wheels", name, version)

        with patch.object(wh, "check_platform"), patch.object(wh, "run_process", side_effect=download):
            self.assertEqual(wh.build("monitoring", output, backend, bootstrap)["profile"],
                             "monitoring")
        with patch.object(wh, "check_platform"), patch.object(wh, "run_process") as run:
            with self.assertRaisesRegex(wh.WheelhouseError, "new_or_empty"):
                wh.build("monitoring", output, backend, bootstrap)
            run.assert_not_called()

    def test_unsafe_index_is_rejected_before_output_or_process(self):
        for index in ("http://example.invalid/simple", "https://user:sentinel@example.invalid/",
                      "https://example.invalid/?token=sentinel", "https://example.invalid/#fragment",
                      "https://example.invalid:8443/simple", "https://example.invalid/\n"):
            with self.subTest(index=index), self.assertRaises(wh.WheelhouseError):
                wh.safe_index(index)
        self.assertEqual(wh.safe_index("https://mirror.example.invalid/simple"),
                         "https://mirror.example.invalid/simple")

    def test_offline_process_cleans_configuration_python_and_routes(self):
        injected = {"PIP_INDEX_URL": "https://example.invalid/", "PIP_EXTRA_INDEX_URL": "sentinel",
                    "PIP_TRUSTED_HOST": "example.invalid", "PIP_TARGET": "bad",
                    "PYTHONPATH": "bad", "PYTHONHOME": "bad", "VIRTUAL_ENV": "old",
                    "http_proxy": "bad", "ALL_PROXY": "bad", "UV_INDEX_URL": "bad",
                    "REQUESTS_CA_BUNDLE": "bad"}
        with patch.dict(os.environ, injected), patch.object(subprocess, "run") as run:
            wh.run_process(["new-python", "-I", "-m", "pip", "check"], offline=True)
        env = run.call_args.kwargs["env"]
        for key in injected:
            if key not in ("ALL_PROXY",):
                self.assertNotIn(key, env)
        self.assertEqual(env["ALL_PROXY"], "")
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(env["NETRC"], os.devnull)
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertEqual(env["PIP_DISABLE_PIP_VERSION_CHECK"], "1")
        self.assertEqual(env["NO_PROXY"], "*")
        self.assertTrue(run.call_args.kwargs["check"])

    def test_platform_boundary_requires_cpython312_linux_amd64_and_glibc(self):
        with patch.object(wh.sys, "platform", "linux"), patch.object(
                wh.sys, "version_info", (3, 12, 1)), patch.object(
                wh.platform, "machine", return_value="x86_64"):
            for libc, version, valid in (("glibc", "2.36", True), ("glibc", "2.39", True),
                                         ("glibc", "2.35", False), ("musl", "1.2", False)):
                with patch.object(wh.platform, "libc_ver", return_value=(libc, version)):
                    if valid:
                        wh.check_platform()
                    else:
                        with self.assertRaises(wh.WheelhouseError):
                            wh.check_platform()
            with patch.object(wh.platform, "libc_ver", return_value=("glibc", "2.39")):
                with self.assertRaises(wh.WheelhouseError):
                    wh.check_platform(building=True)
        with patch.object(wh.sys, "platform", "win32"):
            with self.assertRaises(wh.WheelhouseError):
                wh.check_platform()

    def test_cli_failure_is_bounded_json(self):
        self.wheels["pip"].write_bytes(b"damaged")
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(wh.main(["verify", "--bundle", str(self.bundle)]), 1)
        self.assertFalse(json.loads(error.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
