"""Private, bounded operator state and physically verified GC recovery bundles.

Callers hold ``PrivateState.lock()`` throughout a maintenance operation.  A
bundle contains only ``manifest.json`` and the numbered payloads declared by
its manifest.  Business metadata and transitions are owned by the caller.
"""

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

try:
    from scripts.file_gc_common import (FileGCError, MAX_DOCUMENT_BYTES, MAX_OBJECT_BYTES,
                                       MAX_OBJECTS, MAX_TOTAL_BYTES, require)
except ModuleNotFoundError:  # direct execution with scripts/ on sys.path
    from file_gc_common import (FileGCError, MAX_DOCUMENT_BYTES, MAX_OBJECT_BYTES,
                                MAX_OBJECTS, MAX_TOTAL_BYTES, require)


_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)
_PAYLOAD = re.compile(r"objects/[0-9]{6}\.bin")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCK = "gc.lock"
_REPARSE_POINT = 0x400
_CHUNK_BYTES = 1024 * 1024


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _same_path(opened, current):
    # Windows ctime from fstat/lstat can differ on supported Python releases.
    width = 5 if os.name == "nt" else 6
    return _identity(opened)[:width] == _identity(current)[:width]


def _name(value):
    require(isinstance(value, str) and _NAME.fullmatch(value) is not None
            and not value.endswith((".", " ")) and not _RESERVED.fullmatch(value),
            "gc_path_invalid")
    return value


def _safe_path(value):
    """Reject traversal, symlinks, reparse points, ADS and short-name aliases."""
    try:
        raw = os.fspath(value)
        require(isinstance(raw, str) and bool(raw) and "\0" not in raw
                and not raw.startswith(("\\\\?\\", "\\\\.\\")), "gc_path_invalid")
        # pathlib normalizes away '.', so inspect raw components first.
        require(all(part not in {".", ".."} for part in re.split(r"[/\\]", raw)),
                "gc_path_invalid")
        candidate = Path(raw)
        for part in candidate.parts:
            if part == candidate.anchor:
                continue
            require(not part.endswith((".", " ")) and ":" not in part
                    and not _RESERVED.fullmatch(part), "gc_path_invalid")
        candidate = candidate.absolute()
        for path in (candidate, *candidate.parents):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            require(not stat.S_ISLNK(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & _REPARSE_POINT,
                    "gc_path_invalid")
        require(os.path.normcase(str(candidate)) == os.path.normcase(str(candidate.resolve())),
                "gc_path_invalid")
        return candidate
    except FileGCError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError):
        raise FileGCError("gc_path_invalid") from None


@lru_cache(maxsize=1)
def _windows_user_sid():
    """Read the current process account SID without invoking an external tool."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token, length = wintypes.HANDLE(), wintypes.DWORD()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise OSError
        return _windows_sid_string(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)


def _windows_sid_string(sid):
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _windows_private(path):
    """Accept only owner, administrators and SYSTEM read/write permissions.

    Python 3.13+ honors mkdir(mode=0o700) on Windows.  Checking the DACL also
    protects an existing operator-supplied directory, including on Python 3.12.
    Unknown ACL forms fail closed rather than changing an operator's ACL.
    """
    import ctypes
    from ctypes import wintypes

    class ACL(ctypes.Structure):
        _fields_ = [("revision", wintypes.BYTE), ("reserved", wintypes.BYTE),
                    ("size", wintypes.WORD), ("count", wintypes.WORD),
                    ("reserved2", wintypes.WORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(ctypes.c_void_p)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                         ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise OSError
    try:
        # CPython's Windows 0700 descriptor grants Owner Rights (OW), whose
        # permissions are limited to the owner verified immediately below.
        allowed = {_windows_user_sid(), "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
        if not owner.value or _windows_sid_string(owner) != _windows_user_sid() or not dacl.value:
            return False
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        for index in range(acl.count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 0:  # ACCESS_ALLOWED_ACE; SID follows header and mask.
                if _windows_sid_string(ace.value + 8) not in allowed:
                    return False
            elif kind not in {1, 2, 3}:  # deny, audit and alarm do not grant access.
                return False
        return True
    finally:
        kernel.LocalFree(descriptor)


def _private(path, info, code):
    if os.name == "nt":
        require(_windows_private(path), code)
    else:
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0, code)


def _directory(path, code="gc_path_invalid"):
    _safe_path(path)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), code)
    _private(path, info, code)
    return info


def _regular(path, code="gc_state_invalid"):
    _safe_path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, code)
    _private(path, info, code)
    return info


def _fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_file(path, limit, code):
    before = _regular(path, code)
    require(0 <= before.st_size <= limit, code)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(_same_path(opened, before) and opened.st_nlink == 1
                and stat.S_ISREG(opened.st_mode), code)
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        require(len(raw) <= limit and _identity(opened) == _identity(after)
                and _same_path(after, _regular(path, code)), code)
    return raw


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "gc_state_invalid")
            result[key] = value
        return result

    def constant(value):
        raise ValueError

    def number(value):
        result = float(value)
        require(math.isfinite(result), "gc_state_invalid")
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant,
                       parse_float=number)
    require(isinstance(value, dict), "gc_state_invalid")
    return value


class PrivateState:
    """A private direct-child state store; its parent is operator-provisioned."""

    def __init__(self, root):
        self.root = _safe_path(root)
        try:
            try:
                self.root.mkdir(mode=0o700)
                _fsync_directory(self.root.parent)
            except FileExistsError:
                pass
            self._root_identity = _identity(_directory(self.root))[:2]
        except FileGCError:
            raise
        except OSError:
            raise FileGCError("gc_state_unavailable") from None

    def _path(self, name, *, internal=False):
        require(_identity(_directory(self.root))[:2] == self._root_identity, "gc_path_invalid")
        _name(name)
        require(internal or name != _LOCK, "gc_path_invalid")
        path = _safe_path(self.root / name)
        require(path.parent == self.root, "gc_path_invalid")
        return path

    def read(self, name):
        try:
            path = self._path(name)
            result = _decode(_read_file(path, MAX_DOCUMENT_BYTES, "gc_state_invalid"))
            self._path(name)
            return result
        except FileGCError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise FileGCError("gc_state_invalid") from None
        except OSError:
            raise FileGCError("gc_state_unavailable") from None

    def write(self, name, document):
        temporary, temporary_identity = None, None
        try:
            require(isinstance(document, dict), "gc_state_invalid")
            raw = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
            require(len(raw) <= MAX_DOCUMENT_BYTES, "gc_state_invalid")
            _decode(raw)  # reject e.g. duplicate keys after JSON coerces key types.
            path = self._path(name)
            try:
                previous = _identity(_regular(path))
            except FileNotFoundError:
                previous = None
            descriptor, temporary_name = tempfile.mkstemp(prefix=".gc-state-", dir=self.root)
            temporary = Path(temporary_name)
            temporary_identity = _identity(os.fstat(descriptor))[:2]
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                require(_same_path(os.fstat(stream.fileno()), _regular(temporary)), "gc_state_invalid")
            self._path(name)
            try:
                current = _identity(_regular(path))
            except FileNotFoundError:
                current = None
            require(current == previous, "gc_state_invalid")
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(self.root)
        except FileGCError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise FileGCError("gc_state_invalid") from None
        except OSError:
            raise FileGCError("gc_state_unavailable") from None
        finally:
            if temporary is not None:
                try:
                    if _identity(_regular(temporary))[:2] == temporary_identity:
                        temporary.unlink()
                except (OSError, FileGCError):
                    pass

    def create_bundle(self, bundle_id):
        try:
            path = self._path(bundle_id)
            path.mkdir(mode=0o700)  # exclusive; never reuses an earlier run.
            _directory(path)
            (path / "objects").mkdir(mode=0o700)
            _directory(path / "objects")
            self._path(bundle_id)
            _fsync_directory(path)
            _fsync_directory(self.root)
            return path
        except FileGCError:
            raise
        except OSError:
            raise FileGCError("gc_state_unavailable") from None

    def bundle(self, bundle_id):
        try:
            path = self._path(bundle_id)
            _directory(path)
            _directory(path / "objects")
            self._path(bundle_id)
            return path
        except FileGCError:
            raise
        except OSError:
            raise FileGCError("gc_state_unavailable") from None

    @contextmanager
    def lock(self):
        """Take a stable nonblocking OS lock that survives no crashed process."""
        descriptor = None
        try:
            path = self._path(_LOCK, internal=True)
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _regular(path)
                descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            require(opened.st_size in {0, 1} and opened.st_nlink == 1 and stat.S_ISREG(opened.st_mode)
                    and _same_path(opened, _regular(path)), "gc_state_invalid")
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise FileGCError("gc_locked") from None
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
                _fsync_directory(self.root)
            self._path(_LOCK, internal=True)
            require(_same_path(os.fstat(descriptor), _regular(path)), "gc_state_invalid")
        except FileGCError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise FileGCError("gc_state_unavailable") from None
        try:
            yield self
        finally:
            try:
                os.close(descriptor)
            except OSError:
                raise FileGCError("gc_state_unavailable") from None


def _snapshot(root, expected):
    """Capture the exact flat bundle layout without following arbitrary trees."""
    captured = {"": _identity(_directory(root, "gc_bundle_invalid"))}
    pending = [root]
    count = 0
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            count += 1
            require(count <= MAX_OBJECTS + 2, "gc_bundle_invalid")
            relative = path.relative_to(root).as_posix()
            require(relative in expected, "gc_bundle_invalid")
            if relative == "objects":
                info = _directory(path, "gc_bundle_invalid")
                pending.append(path)
            else:
                info = _regular(path, "gc_bundle_invalid")
                if relative == "manifest.json":
                    require(info.st_size <= MAX_DOCUMENT_BYTES, "gc_bundle_invalid")
            captured[relative] = _identity(info)
    require(set(captured) == expected | {""}, "gc_bundle_invalid")
    return captured


def _hash_file(path, size):
    before = _regular(path, "gc_bundle_invalid")
    require(before.st_size == size, "gc_bundle_invalid")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    digest, total = hashlib.sha256(), 0
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(_same_path(opened, before) and opened.st_nlink == 1, "gc_bundle_invalid")
        while chunk := stream.read(min(_CHUNK_BYTES, size - total + 1)):
            total += len(chunk)
            require(total <= size, "gc_bundle_invalid")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        require(total == size and _identity(opened) == _identity(after)
                and _same_path(after, _regular(path, "gc_bundle_invalid")), "gc_bundle_invalid")
    return digest.hexdigest()


def validate_bundle(path, manifest):
    """Verify every declared recovery payload; never interpret business fields."""
    try:
        root = _safe_path(path)
        require(isinstance(manifest, dict) and isinstance(manifest.get("objects"), list),
                "gc_bundle_invalid")
        require(len(manifest["objects"]) <= MAX_OBJECTS, "gc_bundle_invalid")
        expected, total = {"manifest.json", "objects"}, 0
        for item in manifest["objects"]:
            require(isinstance(item, dict) and isinstance(item.get("payload"), str)
                    and _PAYLOAD.fullmatch(item["payload"]) is not None
                    and type(item.get("size")) is int and 0 <= item["size"] <= MAX_OBJECT_BYTES
                    and isinstance(item.get("sha256"), str)
                    and _SHA256.fullmatch(item["sha256"]) is not None, "gc_bundle_invalid")
            require(item["payload"] not in expected, "gc_bundle_invalid")
            expected.add(item["payload"])
            total += item["size"]
            require(total <= MAX_TOTAL_BYTES, "gc_bundle_invalid")
        before = _snapshot(root, expected)
        try:
            stored = _decode(_read_file(root / "manifest.json", MAX_DOCUMENT_BYTES, "gc_bundle_invalid"))
        except FileGCError:
            raise FileGCError("gc_bundle_invalid") from None
        require(stored == manifest, "gc_bundle_invalid")
        for item in manifest["objects"]:
            require(_hash_file(root / item["payload"], item["size"]) == item["sha256"],
                    "gc_bundle_invalid")
        require(before == _snapshot(root, expected), "gc_bundle_invalid")
    except FileGCError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise FileGCError("gc_bundle_invalid") from None
