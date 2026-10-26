"""Private local scheduling state and conservative, receipt-based retention.

The caller holds ``StateStore.lock()`` across its complete maintenance run and
only calls prune after a new backup and service recovery have both succeeded.
No database, storage service, application secret or recursive deletion is used.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


MAX_STATE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_ENTRIES = 100_003
MAX_BACKUPS = 10_000
_NAME = re.compile(r"scheduled-([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{12}")
_HEX = re.compile(r"[0-9a-f]{64}")
_RECEIPT_FIELDS = {"name", "created_at", "schema_revision", "manifest_sha256"}
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)


class ScheduleError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"schedule_[a-z0-9_]{1,64}", code) else "schedule_failed"
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise ScheduleError(code) from None


def _no_aliases(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "schedule_path_invalid")
    # realpath expands Windows short-name aliases as well as links. Case alone
    # is not an alias on Windows; trailing dots, ADS and device names are below.
    _require(os.path.normcase(str(path)) == os.path.normcase(str(path.resolve())), "schedule_path_invalid")


def private_path(path):
    """Return an absolute path without traversal, links or Windows aliases."""
    try:
        raw = os.fspath(path)
        _require(isinstance(raw, str) and bool(raw) and "\x00" not in raw
                 and not raw.startswith(("\\\\?\\", "\\\\.\\")), "schedule_path_invalid")
        candidate = Path(raw)
        for part in candidate.parts:
            if part == candidate.anchor:
                continue
            _require(part not in {".", ".."} and not part.endswith((".", " "))
                     and ":" not in part and not _RESERVED.fullmatch(part), "schedule_path_invalid")
        candidate = candidate.absolute()
        _no_aliases(candidate)
        return candidate
    except ScheduleError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError):
        raise ScheduleError("schedule_path_invalid") from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _same_path(descriptor, current):
    # Windows ctime differs between fstat and lstat on some Python versions.
    return _identity(descriptor)[:5] == _identity(current)[:5] if os.name == "nt" else _identity(descriptor) == _identity(current)


def _regular(path, code):
    private_path(path)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, code)
    return info


def _read_file(path, limit, code):
    before = _regular(path, code)
    _require(before.st_size <= limit, code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        _require(_same_path(opened, before) and opened.st_nlink == 1, code)
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        _require(len(raw) <= limit and _identity(opened) == _identity(after)
                 and _same_path(after, _regular(path, code)), code)
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "schedule_state_invalid")
        result[key] = value
    return result


def _decode(raw, code):
    try:
        def invalid_constant(value):
            raise ValueError
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError, ScheduleError):
        raise ScheduleError(code) from None


def _directory(path):
    private_path(path)
    _require(stat.S_ISDIR(path.lstat().st_mode), "schedule_path_invalid")


def _mkdir_private(path):
    if not path.exists():
        path.mkdir(mode=0o700)
    _directory(path)
    if os.name != "nt":
        _require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "schedule_state_invalid")


def _fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class StateStore:
    def __init__(self, root):
        self.root = private_path(root)
        try:
            # State root's parent is operator-provisioned; do not create or
            # chmod a whole arbitrary ancestor tree on the caller's behalf.
            _mkdir_private(self.root)
            self._root_identity = _identity(self.root.lstat())[:2]
        except ScheduleError:
            raise
        except OSError:
            raise ScheduleError("schedule_state_unavailable") from None

    def _path(self, name, *, create=False):
        _directory(self.root)
        _require(_identity(self.root.lstat())[:2] == self._root_identity, "schedule_path_invalid")
        _require(isinstance(name, str) and bool(name) and all(
            re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) and part not in {".", ".."}
            and not part.endswith(".") and not _RESERVED.fullmatch(part)
            for part in name.split("/")), "schedule_path_invalid")
        path = private_path(self.root.joinpath(*name.split("/")))
        _require(path.is_relative_to(self.root), "schedule_path_invalid")
        if create:
            for parent in reversed(path.parent.parents):
                if parent.is_relative_to(self.root) and parent != self.root:
                    _mkdir_private(parent)
            _mkdir_private(path.parent)
        else:
            for parent in path.parents:
                if parent.is_relative_to(self.root) and parent.exists():
                    _directory(parent)
        return path

    def read(self, name, default=None):
        try:
            path = self._path(name)
            try:
                return _decode(_read_file(path, MAX_STATE_BYTES, "schedule_state_invalid"), "schedule_state_invalid")
            except FileNotFoundError:
                # A missing state root is not equivalent to an absent leaf.
                self._path(name)
                return default
        except ScheduleError:
            raise
        except (OSError, ValueError, TypeError):
            raise ScheduleError("schedule_state_unavailable") from None

    def write(self, name, value):
        temporary, temporary_identity = None, None
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _require(len(raw) <= MAX_STATE_BYTES, "schedule_state_invalid")
            path = self._path(name, create=True)
            previous = _identity(_regular(path, "schedule_state_invalid")) if path.exists() else None
            descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
            temporary = Path(temporary_name)
            temporary_identity = _identity(os.fstat(descriptor))[:2]
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(name)
            current = _identity(_regular(path, "schedule_state_invalid")) if path.exists() else None
            _require(current == previous, "schedule_state_invalid")
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
        except ScheduleError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ScheduleError("schedule_state_invalid") from None
        except OSError:
            raise ScheduleError("schedule_state_unavailable") from None
        finally:
            if temporary is not None:
                try:
                    if _identity(_regular(temporary, "schedule_state_invalid"))[:2] == temporary_identity:
                        temporary.unlink()
                except (OSError, ScheduleError):
                    pass

    @contextmanager
    def lock(self):
        """Hold a stable, nonblocking OS lock; the OS releases it on exit/crash."""
        descriptor = None
        try:
            path = self._path("schedule.lock")
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _regular(path, "schedule_state_invalid")
                descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            _require(opened.st_size in {0, 1} and opened.st_nlink == 1 and stat.S_ISREG(opened.st_mode)
                     and _identity(opened)[:3] == _identity(_regular(path, "schedule_state_invalid"))[:3],
                     "schedule_state_invalid")
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ScheduleError("schedule_locked") from None
            # Locking beyond EOF is supported by both OS primitives. Initialize
            # only under that lock, so a crash after O_CREAT leaves no stale lock
            # and racing first-use processes still compete on the same inode.
            if os.fstat(descriptor).st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
        except ScheduleError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise ScheduleError("schedule_state_unavailable") from None
        try:
            yield self
        finally:
            # Closing the descriptor releases either OS primitive, including
            # exception paths; the stable lock pathname is never removed.
            try:
                os.close(descriptor)
            except OSError:
                raise ScheduleError("schedule_state_unavailable") from None


def _backup_root(store, backup_root):
    root = private_path(backup_root)
    _directory(root)
    _require(not root.is_relative_to(store.root) and not store.root.is_relative_to(root), "schedule_path_invalid")
    store._path("receipts")
    return root


def _valid_name(name):
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    _require(match is not None, "schedule_receipt_invalid")
    try:
        timestamp = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        _require(timestamp <= datetime.now(timezone.utc) + timedelta(minutes=5), "schedule_receipt_invalid")
    except ValueError:
        raise ScheduleError("schedule_receipt_invalid") from None


def _time(value):
    _require(isinstance(value, str) and len(value) <= 64, "schedule_receipt_invalid")
    try:
        result = datetime.fromisoformat(value)
        _require(result.tzinfo is not None and result.utcoffset() is not None, "schedule_receipt_invalid")
        result = result.astimezone(timezone.utc)
        _require(result <= datetime.now(timezone.utc) + timedelta(minutes=5), "schedule_receipt_invalid")
        return result
    except (ValueError, OverflowError):
        raise ScheduleError("schedule_receipt_invalid") from None


def _manifest(root):
    raw = _read_file(root / "manifest.json", MAX_MANIFEST_BYTES, "schedule_receipt_invalid")
    value = _decode(raw, "schedule_receipt_invalid")
    _require(isinstance(value, dict) and isinstance(value.get("schema_revision"), str)
             and re.fullmatch(r"[A-Za-z0-9_]{1,128}", value["schema_revision"]), "schedule_receipt_invalid")
    _time(value.get("created_at"))
    return value, hashlib.sha256(raw).hexdigest()


def register_backup(store, backup_root, name, manifest):
    try:
        _valid_name(name)
        root = _backup_root(store, backup_root) / name
        _directory(root)
        disk, digest = _manifest(root)
        _require(isinstance(manifest, dict) and manifest == disk, "schedule_receipt_invalid")
        receipt = {"name": name, "created_at": disk["created_at"], "schema_revision": disk["schema_revision"],
                   "manifest_sha256": digest}
        store.write(f"receipts/{name}.json", receipt)
        return receipt
    except ScheduleError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ScheduleError("schedule_receipt_invalid") from None


def _receipt(store, root, name):
    _valid_name(name)
    bundle = private_path(root / name)
    _directory(bundle)
    receipt = store.read(f"receipts/{name}.json")
    _require(isinstance(receipt, dict) and set(receipt) == _RECEIPT_FIELDS
             and receipt["name"] == name and isinstance(receipt["manifest_sha256"], str)
             and _HEX.fullmatch(receipt["manifest_sha256"]), "schedule_receipt_invalid")
    manifest, digest = _manifest(bundle)
    _require(receipt["manifest_sha256"] == digest and receipt["created_at"] == manifest["created_at"]
             and receipt["schema_revision"] == manifest["schema_revision"], "schedule_receipt_invalid")
    return receipt, manifest


def retention_plan(store, backup_root, keep_last):
    _require(type(keep_last) is int and 1 <= keep_last <= 365, "schedule_retention_invalid")
    try:
        root = _backup_root(store, backup_root)
        valid, skipped = [], []
        entries = []
        for entry in root.iterdir():
            _require(len(entries) < MAX_BACKUPS, "schedule_retention_invalid")
            entries.append(entry)
        for entry in sorted(entries, key=lambda path: path.name):
            try:
                receipt, _ = _receipt(store, root, entry.name)
                valid.append((_time(receipt["created_at"]), entry.name))
            except (ScheduleError, OSError, ValueError, TypeError):
                # Arbitrary operator filenames may be private. Only strictly
                # structured scheduled names can escape into runner status.
                display_name = entry.name if _NAME.fullmatch(entry.name) else "unmanaged_backup"
                skipped.append({"name": display_name, "reason": "schedule_receipt_invalid"})
        names = [name for _, name in sorted(valid)]
        return {"candidates": names[:-keep_last], "retained": names[-keep_last:], "skipped": skipped}
    except ScheduleError:
        raise
    except OSError:
        raise ScheduleError("schedule_retention_unavailable") from None


def _bundle_snapshot(root, manifest):
    """Capture exactly the files accepted by the existing complete verifier."""
    _require(isinstance(manifest.get("dump"), dict) and manifest["dump"].get("path") == "database.dump"
             and isinstance(manifest.get("objects"), list), "schedule_backup_invalid")
    expected = {"manifest.json", "database.dump", "objects"}
    for item in manifest["objects"]:
        _require(isinstance(item, dict) and item.get("status") in {"present", "missing"}
                 and isinstance(item.get("path"), str)
                 and re.fullmatch(r"objects/[0-9a-f]{64}", item["path"]), "schedule_backup_invalid")
        if item["status"] == "present":
            _require(item["path"] not in expected, "schedule_backup_invalid")
            expected.add(item["path"])
    _require(len(expected) <= MAX_BUNDLE_ENTRIES, "schedule_backup_invalid")
    _directory(root)
    actual = {"": _identity(root.lstat())}
    pending = [root]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            private_path(path)
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            _require(relative in expected, "schedule_backup_invalid")
            if relative == "objects":
                _require(stat.S_ISDIR(info.st_mode), "schedule_backup_invalid")
                pending.append(path)
            else:
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "schedule_backup_invalid")
            actual[relative] = _identity(info)
    _require(set(actual) == expected | {""}, "schedule_backup_invalid")
    return actual


def _verified_bundle(store, root, name, verify):
    receipt, manifest = _receipt(store, root, name)
    bundle = root / name
    before = _bundle_snapshot(bundle, manifest)
    verified = verify(bundle)
    _require(isinstance(verified, dict) and verified == manifest, "schedule_backup_invalid")
    _require(before == _bundle_snapshot(bundle, manifest), "schedule_backup_changed")
    _require(_receipt(store, root, name) == (receipt, manifest), "schedule_backup_changed")
    return receipt, manifest, before


def _unchanged_bundle(store, root, name, captured):
    receipt, manifest, before = captured
    _require(before == _bundle_snapshot(root / name, manifest), "schedule_backup_changed")
    _require(_receipt(store, root, name) == (receipt, manifest), "schedule_backup_changed")


def _delete_bundle(store, backup_root, name, captured, root_identity):
    root = _backup_root(store, backup_root)
    _require(_identity(root.lstat())[:2] == root_identity, "schedule_backup_changed")
    _unchanged_bundle(store, root, name, captured)
    _, _, before = captured
    bundle = root / name
    files = sorted(relative for relative in before if relative not in {"", "objects"})
    # Manifest is checked again immediately before the first irreversible step;
    # keeping it until last also leaves an identifiable bundle after I/O failure.
    files.remove("manifest.json")
    files.append("manifest.json")
    for relative in files:
        _require(_identity(_backup_root(store, root).lstat())[:2] == root_identity,
                 "schedule_backup_changed")
        _require(_identity(bundle.lstat())[:2] == before[""][:2]
                 and _identity((bundle / "objects").lstat())[:2] == before["objects"][:2], "schedule_backup_changed")
        path = bundle / relative
        _require(_identity(_regular(path, "schedule_backup_changed")) == before[relative], "schedule_backup_changed")
        path.unlink()
    private_path(bundle / "objects").rmdir()
    private_path(bundle).rmdir()
    _fsync_directory(root)


def prune_backups(store, backup_root, keep_last, verify):
    """Prune old verified receipts; caller must first finish backup and recovery."""
    root = _backup_root(store, backup_root)
    root_identity = _identity(root.lstat())[:2]
    plan = retention_plan(store, backup_root, keep_last)
    result = {"deleted": [], "retained": [], "skipped": list(plan["skipped"])}
    verified = {}
    for name in plan["candidates"] + plan["retained"]:
        try:
            _require(_identity(_backup_root(store, root).lstat())[:2] == root_identity, "schedule_backup_changed")
            verified[name] = _verified_bundle(store, root, name, verify)
        except Exception:
            result["skipped"].append({"name": name, "reason": "schedule_backup_invalid"})
    names = list(verified)
    result["retained"] = names[-keep_last:]
    candidates = names[:-keep_last]
    # A damaged newest receipt must not displace the last usable older copy.
    # Validate all protected identities once more before any destructive work.
    try:
        for name in result["retained"]:
            _unchanged_bundle(store, root, name, verified[name])
    except Exception:
        for name in candidates:
            result["skipped"].append({"name": name, "reason": "schedule_backup_changed"})
        candidates = []
    for name in candidates:
        try:
            _delete_bundle(store, root, name, verified[name], root_identity)
            result["deleted"].append(name)
        except Exception:
            # Verifier errors may contain private data. Only a fixed reason
            # escapes this boundary, including unsupported historical schemas.
            result["skipped"].append({"name": name, "reason": "schedule_backup_invalid"})
    result["skipped"].sort(key=lambda item: item["name"])
    return result
