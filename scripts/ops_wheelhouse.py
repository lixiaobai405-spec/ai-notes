"""Build, inspect and install a bounded, offline Linux operations wheelhouse.

Only build contacts an explicitly selected HTTPS package index. Verification is
portable and uses only the standard library. SHA-256 detects damaged contents;
trust the script and manifest via the original commit/workflow artifact source.
"""

from __future__ import annotations

import argparse
import ast
from email.parser import Parser
import hashlib
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile


TARGET = {"os": "linux", "architecture": "x86_64", "implementation": "cpython",
          "python": "3.12", "libc": "glibc", "minimum_glibc": "2.36"}
PROFILES = ("monitoring", "backup")
MONITORING = frozenset(("sqlalchemy", "psycopg", "psycopg-binary", "typing-extensions"))
MAX_MANIFEST = 64 * 1024
MAX_FILES = 128
MAX_FILE = 200 * 1024 * 1024
MAX_TOTAL = 750 * 1024 * 1024
MAX_TEXT = 128 * 1024
# Distribution METADATA includes package descriptions and may exceed lock sizes.
MAX_WHEEL_METADATA = 1024 * 1024
FIXED_FILES = frozenset(("install.py", "backend-requirements.lock", "bootstrap.lock",
                         "requirements.lock", "requirements-offline.txt",
                         "bootstrap-offline.txt"))
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9.!+_-]*)(?:\s*;\s*(.+))?")
SHA = re.compile(r"[0-9a-f]{64}")
SAFE_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]*\.whl")
MARKERS = {"implementation_name": "cpython", "platform_python_implementation": "CPython",
           "sys_platform": "linux", "platform_system": "Linux",
           "platform_machine": "x86_64", "os_name": "posix",
           "python_version": "3.12"}
SMOKE_IMPORTS = {
    "monitoring": ("sqlalchemy", "psycopg", "psycopg_binary", "typing_extensions"),
    "backup": ("sqlalchemy", "psycopg", "alembic", "alibabacloud_oss_v2", "fastapi",
               "pydantic_settings", "PIL", "pypdfium2", "argon2", "httpx",
               "markdown_it", "mdit_py_plugins", "uvicorn", "yaml", "uvloop",
               "watchfiles", "websockets", "Crypto"),
}


class WheelhouseError(ValueError):
    """Invalid input or unsafe/nonmatching offline package."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT:
        raise WheelhouseError("text_file_too_large")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise WheelhouseError("invalid_utf8") from exc


def marker_applies(expression: str) -> bool:
    """Evaluate only the small explicit marker grammar used by our locked input.

    Unknown markers/operators fail closed so a future lock update needs review.
    No application settings or host environment values participate.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise WheelhouseError("unsupported_lock_marker") from exc

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name) and node.id in MARKERS:
            return MARKERS[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [visit(value) for value in node.values]
            if not all(type(value) is bool for value in values):
                raise WheelhouseError("unsupported_lock_marker")
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if not isinstance(left, str) or not isinstance(right, str):
                raise WheelhouseError("unsupported_lock_marker")
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise WheelhouseError("unsupported_lock_marker")

    result = visit(tree)
    if type(result) is not bool:
        raise WheelhouseError("unsupported_lock_marker")
    return result


def parse_lock(text: str) -> dict[str, str]:
    if len(text) > MAX_TEXT:
        raise WheelhouseError("lock_too_large")
    pins = {}
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise WheelhouseError("lock_requires_exact_pins")
        name, version, marker = match.groups()
        name = canonical(name)
        if name in seen:
            raise WheelhouseError("duplicate_lock_package")
        seen.add(name)
        if marker is None or marker_applies(marker):
            pins[name] = version
    if not pins or len(seen) > MAX_FILES - 8:
        raise WheelhouseError("invalid_lock_package_count")
    return pins


def profile_pins(text: str, profile: str) -> dict[str, str]:
    if profile not in PROFILES:
        raise WheelhouseError("invalid_profile")
    pins = parse_lock(text)
    if profile == "monitoring":
        if not MONITORING <= pins.keys():
            raise WheelhouseError("monitoring_pins_missing")
        return {name: pins[name] for name in sorted(MONITORING)}
    if "pip" in pins:
        raise WheelhouseError("bootstrap_package_in_application_lock")
    return dict(sorted(pins.items()))


def pins_text(pins: dict[str, str]) -> str:
    return "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))


def check_platform(*, building: bool = False) -> None:
    libc, version = platform.libc_ver()
    try:
        glibc = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:
        glibc = ()
    if (sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64")
            or sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12)
            or libc != "glibc" or len(glibc) != 2 or glibc < (2, 36)
            or (building and glibc != (2, 36))):
        raise WheelhouseError("requires_linux_x86_64_cpython312_glibc236"
                              + ("_build_baseline" if building else "_or_newer"))


def clean_environment(*, offline: bool) -> dict[str, str]:
    """Discard inherited pip configuration, Python injection and proxy routes."""
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(("PIP_", "PYTHON", "UV_"))
           and name.upper() not in ("VIRTUAL_ENV", "__PYVENV_LAUNCHER__",
                                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NETRC")}
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1", "NETRC": os.devnull})
    if offline:
        env.update({"PIP_NO_INDEX": "1", "NO_PROXY": "*",
                    "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": ""})
    return env


def run_process(command: list[str], *, offline: bool) -> None:
    try:
        subprocess.run(command, check=True, env=clean_environment(offline=offline),
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        # Avoid echoing inherited environment or index response credentials.
        raise WheelhouseError("wheelhouse_process_failed") from exc


def safe_index(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port not in (None, 443) or "\\" in url
                or any(ord(char) <= 32 or ord(char) >= 127 for char in url)):
            raise ValueError
    except ValueError as exc:
        raise WheelhouseError("index_requires_https_without_credentials_query_or_fragment") from exc
    return url


def reject_symlink_parents(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise WheelhouseError("symlink_path")


def resolve_bundle(path: Path) -> Path:
    """Allow relative/parent components, but compare the real bundle location.

    Reject links before resolving: normalization must not erase a link hidden
    in a path such as alias/../bundle. The resulting canonical path is shared
    by verification and the install destination overlap checks.
    """
    path = path.absolute()
    reject_symlink_parents(path)
    return path.resolve(strict=True)


def new_output(path: Path) -> Path:
    path = path.absolute()
    reject_symlink_parents(path)
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise WheelhouseError("output_must_be_new_or_empty")
    else:
        path.mkdir(parents=True)
    return path


def compatible_tag(python_tag: str, abi: str, tag: str) -> bool:
    if tag == "any":
        return python_tag in ("py3", "py312") and abi == "none"
    if tag == "linux_x86_64" or tag in ("manylinux1_x86_64", "manylinux2010_x86_64",
                                       "manylinux2014_x86_64"):
        platform_ok = True
    else:
        match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
        platform_ok = bool(match and (int(match[1]), int(match[2])) <= (2, 36))
    if not platform_ok:
        return False
    if python_tag in ("py3", "py312"):
        return abi == "none"
    if python_tag == "cp312":
        return abi in ("cp312", "abi3", "none")
    match = re.fullmatch(r"cp3(\d+)", python_tag)
    return bool(match and 2 <= int(match[1]) <= 12 and abi == "abi3")


def wheel_identity(path: Path) -> tuple[str, str]:
    if not SAFE_WHEEL.fullmatch(path.name):
        raise WheelhouseError("invalid_wheel_filename")
    parts = path.name[:-4].split("-")
    if len(parts) not in (5, 6) or (len(parts) == 6 and not re.fullmatch(r"\d[0-9A-Za-z_]*", parts[2])):
        raise WheelhouseError("invalid_wheel_filename")
    name, version = canonical(parts[0]), parts[1]
    tags = [part.split(".") for part in parts[-3:]]
    filename_tags = {"-".join(tag) for tag in itertools.product(*tags)}
    if not any(compatible_tag(*tag.split("-")) for tag in filename_tags):
        raise WheelhouseError("incompatible_wheel_tag")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 30000 or sum(info.file_size for info in infos) > 1024 ** 3:
                raise WheelhouseError("wheel_archive_too_large")
            seen = set()
            for info in infos:
                member = PurePosixPath(info.filename)
                if (info.filename in seen or member.is_absolute() or ".." in member.parts
                        or "\\" in info.filename or ":" in info.filename
                        or member.as_posix() != info.filename.rstrip("/")
                        or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                    raise WheelhouseError("unsafe_or_duplicate_wheel_member")
                seen.add(info.filename)
            metadata_names = [info.filename for info in infos
                              if info.filename.endswith(".dist-info/METADATA")]
            expected = f"{parts[0]}-{version}.dist-info/METADATA"
            if (metadata_names != [expected]
                    or archive.getinfo(expected).file_size > MAX_WHEEL_METADATA):
                raise WheelhouseError("invalid_wheel_metadata")
            metadata = Parser().parsestr(archive.read(expected).decode("utf-8"))
            if (len(metadata.get_all("Name", [])) != 1
                    or len(metadata.get_all("Version", [])) != 1
                    or canonical(metadata["Name"]) != name or metadata["Version"] != version):
                raise WheelhouseError("wheel_metadata_identity_mismatch")
            wheel_path = expected.removesuffix("METADATA") + "WHEEL"
            if archive.getinfo(wheel_path).file_size > MAX_TEXT:
                raise WheelhouseError("invalid_wheel_metadata")
            wheel = Parser().parsestr(archive.read(wheel_path).decode("utf-8"))
            declared_tags = wheel.get_all("Tag", [])
            if (wheel.get_all("Wheel-Version", []) != ["1.0"]
                    or wheel.get_all("Root-Is-Purelib", []) not in (["true"], ["false"])
                    or not declared_tags or len(declared_tags) != len(set(declared_tags))
                    or not set(declared_tags) <= filename_tags):
                raise WheelhouseError("invalid_wheel_metadata")
            archive.getinfo(expected.removesuffix("METADATA") + "RECORD")
    except (zipfile.BadZipFile, KeyError, UnicodeError, OSError) as exc:
        raise WheelhouseError("invalid_wheel_archive") from exc
    return name, version


def wheel_inventory(bundle: Path, expected: dict[str, str]) -> dict[str, Path]:
    wheels = {}
    for path in sorted((bundle / "wheels").iterdir()):
        if path.is_symlink() or not path.is_file():
            raise WheelhouseError("invalid_wheel_entry")
        name, version = wheel_identity(path)
        if name not in expected or expected[name] != version or name in wheels:
            raise WheelhouseError("unexpected_or_duplicate_wheel")
        wheels[name] = path
    if wheels.keys() != expected.keys():
        raise WheelhouseError("missing_wheel")
    return wheels


def hashed_requirements(pins: dict[str, str], wheels: dict[str, Path]) -> str:
    return "".join(f"{name}=={version} --hash=sha256:{digest(wheels[name])}\n"
                   for name, version in sorted(pins.items()))


def inventory(bundle: Path) -> dict[str, dict]:
    if not bundle.is_dir():
        raise WheelhouseError("bundle_directory_missing")
    reject_symlink_parents(bundle)
    files = {}
    total = 0
    for directory, dirs, names in os.walk(bundle, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            child = parent / name
            if child.is_symlink() or child.relative_to(bundle).as_posix() != "wheels":
                raise WheelhouseError("unexpected_or_symlink_directory")
        for name in names:
            path = parent / name
            relative = path.relative_to(bundle).as_posix()
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise WheelhouseError("nonregular_bundle_file")
            if relative == "manifest.json":
                if metadata.st_size > MAX_MANIFEST:
                    raise WheelhouseError("manifest_too_large")
                continue
            if relative not in FIXED_FILES and not (
                    relative.startswith("wheels/") and SAFE_WHEEL.fullmatch(name)):
                raise WheelhouseError("unexpected_bundle_file")
            total += metadata.st_size
            if metadata.st_size > MAX_FILE or total > MAX_TOTAL or len(files) >= MAX_FILES:
                raise WheelhouseError("bundle_size_limit")
            files[relative] = {"size": metadata.st_size, "sha256": digest(path)}
    if not FIXED_FILES <= files.keys() or not (bundle / "wheels").is_dir():
        raise WheelhouseError("bundle_files_missing")
    return files


def unique_json(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise WheelhouseError("duplicate_manifest_key")
        result[name] = value
    return result


def verify(bundle: Path) -> dict:
    bundle = resolve_bundle(bundle)
    files = inventory(bundle)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise WheelhouseError("manifest_missing")
    try:
        manifest = json.loads(read_text(manifest_path), object_pairs_hook=unique_json)
    except (ValueError, RecursionError) as exc:
        raise WheelhouseError("invalid_manifest") from exc
    keys = {"schema_version", "profile", "target", "backend_lock_sha256",
            "bootstrap_lock_sha256", "files"}
    if (not isinstance(manifest, dict) or manifest.keys() != keys
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["profile"] not in PROFILES or manifest["target"] != TARGET
            or not isinstance(manifest["files"], dict)
            or not 1 <= len(manifest["files"]) <= MAX_FILES):
        raise WheelhouseError("unsupported_manifest")
    for name, record in manifest["files"].items():
        # Only inventory's closed set of paths is ever opened.
        if (name not in files or not isinstance(record, dict)
                or record.keys() != {"size", "sha256"}
                or type(record["size"]) is not int or not 0 <= record["size"] <= MAX_FILE
                or not isinstance(record["sha256"], str) or not SHA.fullmatch(record["sha256"])):
            raise WheelhouseError("invalid_manifest_file")
    if manifest["files"] != files:
        raise WheelhouseError("bundle_integrity_mismatch")
    if (manifest["backend_lock_sha256"] != digest(bundle / "backend-requirements.lock")
            or manifest["bootstrap_lock_sha256"] != digest(bundle / "bootstrap.lock")):
        raise WheelhouseError("source_lock_digest_mismatch")
    bootstrap = parse_lock(read_text(bundle / "bootstrap.lock"))
    if bootstrap != {"pip": "24.0"}:
        raise WheelhouseError("unsupported_bootstrap_pin")
    pins = profile_pins(read_text(bundle / "backend-requirements.lock"), manifest["profile"])
    if read_text(bundle / "requirements.lock") != pins_text(pins):
        raise WheelhouseError("profile_lock_mismatch")
    wheels = wheel_inventory(bundle, {**pins, **bootstrap})
    if (read_text(bundle / "requirements-offline.txt") != hashed_requirements(pins, wheels)
            or read_text(bundle / "bootstrap-offline.txt") != hashed_requirements(bootstrap, wheels)):
        raise WheelhouseError("offline_hash_requirements_mismatch")
    return manifest


def build(profile: str, output: Path, backend_lock: Path, bootstrap_lock: Path,
          index_url: str = "https://pypi.org/simple") -> dict:
    check_platform(building=True)
    index_url = safe_index(index_url)
    backend_text, bootstrap_text = read_text(backend_lock), read_text(bootstrap_lock)
    pins = profile_pins(backend_text, profile)
    bootstrap = parse_lock(bootstrap_text)
    if bootstrap != {"pip": "24.0"}:
        raise WheelhouseError("unsupported_bootstrap_pin")
    output = new_output(output)
    (output / "wheels").mkdir()
    (output / "backend-requirements.lock").write_bytes(backend_lock.read_bytes())
    (output / "bootstrap.lock").write_bytes(bootstrap_lock.read_bytes())
    (output / "requirements.lock").write_text(pins_text(pins), encoding="utf-8", newline="\n")
    shutil.copyfile(Path(__file__), output / "install.py")
    run_process([sys.executable, "-I", "-m", "pip", "--disable-pip-version-check",
                 "--no-input", "download", "--only-binary=:all:", "--no-deps",
                 "--no-cache-dir", "--index-url", index_url, "--dest", str(output / "wheels"),
                 "-r", str(output / "requirements.lock"), "-r", str(output / "bootstrap.lock")],
                offline=False)
    wheels = wheel_inventory(output, {**pins, **bootstrap})
    (output / "requirements-offline.txt").write_text(
        hashed_requirements(pins, wheels), encoding="utf-8", newline="\n")
    (output / "bootstrap-offline.txt").write_text(
        hashed_requirements(bootstrap, wheels), encoding="utf-8", newline="\n")
    manifest = {"schema_version": 1, "profile": profile, "target": TARGET,
                "backend_lock_sha256": digest(output / "backend-requirements.lock"),
                "bootstrap_lock_sha256": digest(output / "bootstrap.lock"),
                "files": inventory(output)}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8", newline="\n")
    return verify(output)


def install(bundle: Path, target: Path) -> dict:
    # Even platform/target errors must never bypass validation before installation.
    bundle = resolve_bundle(bundle)
    manifest = verify(bundle)
    check_platform()
    if not target.is_absolute() or ".." in target.parts:
        raise WheelhouseError("venv_requires_absolute_new_path")
    reject_symlink_parents(target)
    if target.exists():
        raise WheelhouseError("venv_already_exists")
    if not target.parent.is_dir():
        raise WheelhouseError("venv_parent_missing")
    if target == bundle or bundle in target.parents or target in bundle.parents:
        raise WheelhouseError("venv_overlaps_bundle")
    # An exclusive mkdir also rejects races creating the target after preflight.
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise WheelhouseError("venv_already_exists") from exc
    run_process([sys.executable, "-I", "-m", "venv", "--copies", "--without-pip", str(target)],
                offline=True)
    python = str(target / "bin/python")
    wheels = wheel_inventory(bundle, {"pip": "24.0", **parse_lock(read_text(bundle / "requirements.lock"))})
    offline = ["--disable-pip-version-check", "--no-input", "install", "--no-index",
               "--find-links", str(bundle / "wheels"), "--only-binary=:all:",
               "--require-hashes", "--no-deps", "--no-cache-dir"]
    # -I ignores PYTHONPATH/user-site; importing pip directly from the verified
    # wheel works without system pip, ensurepip, setuptools, or network access.
    bootstrap_code = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                      "runpy.run_module('pip',run_name='__main__')")
    run_process([python, "-I", "-c", bootstrap_code, str(wheels["pip"]), *offline,
                 "-r", str(bundle / "bootstrap-offline.txt")], offline=True)
    run_process([python, "-I", "-m", "pip", *offline,
                 "-r", str(bundle / "requirements-offline.txt")], offline=True)
    run_process([python, "-I", "-m", "pip", "--disable-pip-version-check", "check"], offline=True)
    smoke = "import importlib; " + "; ".join(
        f"importlib.import_module({name!r})" for name in SMOKE_IMPORTS[manifest["profile"]])
    run_process([python, "-I", "-c", smoke], offline=True)
    return {"profile": manifest["profile"], "venv": str(target), "offline": True}


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="download binary wheels on baseline Linux only")
    builder.add_argument("--profile", choices=PROFILES, required=True)
    builder.add_argument("--output", type=Path, required=True)
    builder.add_argument("--backend-lock", type=Path, default=root / "backend/requirements.lock")
    builder.add_argument("--bootstrap-lock", type=Path, default=root / "ops/wheelhouse/bootstrap.lock")
    builder.add_argument("--index-url", default="https://pypi.org/simple")
    verifier = commands.add_parser("verify", help="portable integrity and metadata verification")
    verifier.add_argument("--bundle", type=Path, required=True)
    installer = commands.add_parser("install", help="verify and install into a NEW absolute venv")
    installer.add_argument("--bundle", type=Path, required=True)
    installer.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(args.profile, args.output, args.backend_lock, args.bootstrap_lock,
                           args.index_url)
            result = {"profile": result["profile"], "files": len(result["files"])}
        elif args.command == "verify":
            manifest = verify(args.bundle)
            result = {"profile": manifest["profile"], "files": len(manifest["files"])}
        else:
            result = install(args.bundle, args.venv)
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (WheelhouseError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, WheelhouseError)
                          else "wheelhouse_filesystem_error"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
