"""Bounded operator-only object inventory and reversible cleanup adapters.

Writers must remain stopped around every destructive operation. Metadata checks
detect drift; neither local unlink nor OSS DeleteObject supplies a compare/delete
primitive. Unknown object names are included in inventory, never normalized.

POSIX archive publication fsyncs both payload and its parent directory. Windows
has no portable directory fsync in this adapter: file data is flushed, but an
equivalent power-loss guarantee is not claimed. Production execution targets
Ubuntu; Windows tests cover logical recovery and interruption handling only.
"""

from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from uuid import uuid4

try:
    from scripts.file_gc_common import FileGCError, MAX_OBJECTS, MAX_OBJECT_BYTES, ObjectRecord, require
except ModuleNotFoundError:  # Direct script execution from the scripts directory.
    from file_gc_common import FileGCError, MAX_OBJECTS, MAX_OBJECT_BYTES, ObjectRecord, require


CHUNK_BYTES = 256 * 1024
_WINDOWS = os.name == "nt"
_KEY = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)


def _noop():
    pass


def _key(value):
    require(isinstance(value, str) and len(value) <= 1024 and _KEY.fullmatch(value)
            and all(part not in {".", ".."} and not part.endswith((".", " "))
                    and not _RESERVED.fullmatch(part) for part in value.split("/")),
            "gc_object_key_invalid")
    return value


def _text(value, maximum=1024):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _iso(value):
    require(isinstance(value, datetime) and value.tzinfo is not None, "gc_storage_metadata_invalid")
    return value.astimezone(timezone.utc).isoformat()


def _path(value):
    raw = Path(value)
    require(".." not in raw.parts, "gc_storage_path_invalid")
    path = raw.absolute()
    for part in path.parts:
        if part != path.anchor:
            require(not part.endswith((".", " ")) and ":" not in part
                    and not _RESERVED.fullmatch(part), "gc_storage_path_invalid")
    _no_aliases(path)
    return path


def _no_aliases(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "gc_storage_path_invalid")


def _regular(info):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400,
            "gc_storage_path_invalid")


def _local_identity(info):
    # Windows fstat/path.stat have different ctime semantics.
    return {"device": info.st_dev, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}


def _local_record(key, info):
    _regular(info)
    require(type(info.st_size) is int and info.st_size >= 0, "gc_storage_metadata_invalid")
    return ObjectRecord(key, info.st_size,
                        datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                        _local_identity(info))


def _read_open(path):
    path = _path(path)
    before = path.lstat()
    _regular(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    try:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        require(_local_identity(before) == _local_identity(opened) and before.st_size == opened.st_size,
                "gc_object_changed")
        return stream
    except BaseException:
        stream.close()
        raise


def _same(first, second):
    require(isinstance(first, ObjectRecord) and first == second, "gc_object_changed")


def _remove_created(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise FileGCError("gc_cleanup_failed") from None


def _fsync_directory(path):
    """Persist a POSIX directory entry; Windows has no portable equivalent here."""
    if _WINDOWS:
        return
    descriptor = None
    try:
        directory = _path(path)
        before = directory.lstat()
        require(stat.S_ISDIR(before.st_mode), "gc_storage_path_invalid")
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        require(stat.S_ISDIR(opened.st_mode) and (before.st_dev, before.st_ino)
                == (opened.st_dev, opened.st_ino), "gc_storage_path_invalid")
        os.fsync(descriptor)
        _no_aliases(directory)
        after = directory.lstat()
        require((after.st_dev, after.st_ino) == (opened.st_dev, opened.st_ino), "gc_storage_path_invalid")
    except FileGCError:
        raise
    except OSError:
        raise FileGCError("gc_directory_sync_failed") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise FileGCError("gc_directory_sync_failed") from None


def _publish_noreplace(stage, target):
    """Atomically move a sibling stage to a missing name, never using hard links."""
    try:
        stage, target = _path(stage), _path(target)
        require(stage.parent == target.parent, "gc_storage_path_invalid")
        _regular(stage.lstat())
        if _WINDOWS:
            # Unlike POSIX rename, Windows rename fails when target exists.
            os.rename(stage, target)
            return
        if not sys.platform.startswith("linux"):
            raise FileGCError("gc_atomic_publish_unsupported")
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        require(rename is not None, "gc_atomic_publish_unsupported")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        # AT_FDCWD = -100, RENAME_NOREPLACE = 1. No check-then-replace fallback.
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError("object_exists")
            if code in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV}:
                raise FileGCError("gc_atomic_publish_unsupported")
            raise FileGCError("gc_restore_publish_failed")
    except (FileGCError, FileExistsError):
        raise
    except OSError:
        raise FileGCError("gc_restore_publish_failed") from None


def _capture(chunks, destination, expected_size, checkpoint=_noop):
    checkpoint()
    require(type(expected_size) is int and 0 <= expected_size <= MAX_OBJECT_BYTES, "gc_object_too_large")
    created = None
    try:
        target = _path(destination)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = target
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as stream:
            for chunk in chunks:
                checkpoint()
                require(isinstance(chunk, bytes), "gc_storage_unavailable")
                size += len(chunk)
                require(size <= expected_size, "gc_object_changed")
                stream.write(chunk)
                digest.update(chunk)
            require(size == expected_size, "gc_object_changed")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(target.parent)
        checkpoint()
        return {"size": size, "sha256": digest.hexdigest()}
    except BaseException as error:
        if created is not None:
            _remove_created(created)
        if isinstance(error, FileGCError) or not isinstance(error, Exception):
            raise
        raise FileGCError("gc_export_failed") from None


def _payload(payload, size, sha256, checkpoint=_noop):
    checkpoint()
    require(type(size) is int and 0 <= size <= MAX_OBJECT_BYTES
            and isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256), "gc_archive_invalid")
    stream = _read_open(payload)
    try:
        info = os.fstat(stream.fileno())
        require(info.st_size == size, "gc_archive_invalid")
        digest, actual = hashlib.sha256(), 0
        while chunk := stream.read(CHUNK_BYTES):
            checkpoint()
            actual += len(chunk)
            require(actual <= size, "gc_archive_invalid")
            digest.update(chunk)
        require(actual == size and digest.hexdigest() == sha256
                and _local_identity(info) == _local_identity(os.fstat(stream.fileno())), "gc_archive_invalid")
        checkpoint()
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def _matches(chunks, size, sha256, checkpoint=_noop):
    checkpoint()
    digest, actual = hashlib.sha256(), 0
    for chunk in chunks:
        checkpoint()
        require(isinstance(chunk, bytes), "gc_storage_unavailable")
        actual += len(chunk)
        require(actual <= size, "gc_restore_conflict")
        digest.update(chunk)
    require(actual == size and digest.hexdigest() == sha256, "gc_restore_conflict")
    checkpoint()


class LocalGCStorage:
    def __init__(self, root):
        self.checkpoint = _noop
        try:
            self.root = _path(root)
            require(self.root.is_dir(), "gc_storage_path_invalid")
            self.root = self.root.resolve()
        except FileGCError:
            raise
        except (OSError, TypeError, ValueError, RuntimeError):
            raise FileGCError("gc_storage_path_invalid") from None

    def backend_identity(self):
        return {"backend": "local", "root": str(self.root)}

    def _path(self, key):
        path = _path(self.root.joinpath(*_key(key).split("/")))
        require(path.resolve().is_relative_to(self.root), "gc_storage_path_invalid")
        return path

    def inventory(self, limit=MAX_OBJECTS):
        self.checkpoint()
        require(type(limit) is int and 0 < limit <= MAX_OBJECTS, "gc_inventory_limit")
        result = []
        try:
            _no_aliases(self.root)
            pending = [self.root]
            while pending:
                self.checkpoint()
                directory = pending.pop()
                _no_aliases(directory)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        self.checkpoint()
                        info = entry.stat(follow_symlinks=False)
                        require(not entry.is_symlink()
                                and not getattr(info, "st_file_attributes", 0) & 0x400,
                                "gc_storage_path_invalid")
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        else:
                            key = Path(entry.path).relative_to(self.root).as_posix()
                            # Windows DirEntry.stat caches incomplete inode/link
                            # metadata; a path lstat provides the real link count.
                            result.append(_local_record(key, Path(entry.path).lstat()))
                            require(len(result) <= limit, "gc_inventory_limit")
            return sorted(result, key=lambda item: item.key)
        except FileGCError:
            raise
        except (OSError, TypeError, ValueError, RuntimeError):
            raise FileGCError("gc_inventory_incomplete") from None

    def head(self, key):
        self.checkpoint()
        try:
            result = _local_record(key, self._path(key).lstat())
            self.checkpoint()
            return result
        except FileNotFoundError:
            raise FileNotFoundError("object_not_found") from None
        except FileGCError:
            raise
        except (OSError, TypeError, ValueError, RuntimeError):
            raise FileGCError("gc_storage_unavailable") from None

    def export(self, record, destination):
        self.checkpoint()
        copied = None
        try:
            _same(record, self.head(record.key))
            with _read_open(self._path(record.key)) as source:
                before = _local_record(record.key, os.fstat(source.fileno()))
                _same(record, before)
                copied = _capture(iter(lambda: source.read(CHUNK_BYTES), b""), destination, record.size,
                                  self.checkpoint)
                _same(record, _local_record(record.key, os.fstat(source.fileno())))
                _same(record, self.head(record.key))
            return copied
        except BaseException as error:
            if copied is not None:
                _remove_created(Path(destination))
            if isinstance(error, (FileGCError, FileNotFoundError)) or not isinstance(error, Exception):
                raise
            raise FileGCError("gc_export_failed") from None

    def ensure_writable(self):
        self.checkpoint()
        created = None
        try:
            _no_aliases(self.root)
            require(self.root.is_dir(), "gc_storage_path_invalid")
            candidate = self.root / (".file-gc-probe-" + uuid4().hex)
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = candidate
            os.close(descriptor)
        except FileGCError:
            raise
        except OSError:
            raise FileGCError("gc_storage_not_writable") from None
        finally:
            if created is not None:
                _remove_created(created)

    def delete(self, record):
        self.checkpoint()
        try:
            _same(record, self.head(record.key))
            self.checkpoint()
            self._path(record.key).unlink()
            # Once unlink succeeds, return its receipt without another budget
            # callback hiding that known outcome.
            return {"backend": "local", "status": "deleted", "key": record.key}
        except FileGCError:
            raise
        except FileNotFoundError:
            raise FileGCError("gc_object_changed") from None
        except OSError:
            raise FileGCError("gc_delete_unknown") from None

    def restore(self, key, payload, *, size, sha256, original=None, receipt=None):
        self.checkpoint()
        created = None
        try:
            target = self._path(key)
            with _payload(payload, size, sha256, self.checkpoint) as source:
                try:
                    self.head(key)
                except FileNotFoundError:
                    pass
                else:
                    return self._existing(key, size, sha256)
                target.parent.mkdir(parents=True, exist_ok=True)
                target = self._path(key)
                stage = target.parent / (".file-gc-restore-" + uuid4().hex)
                descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
                created = stage
                digest, actual = hashlib.sha256(), 0
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                        self.checkpoint()
                        actual += len(chunk)
                        require(actual <= size, "gc_archive_invalid")
                        output.write(chunk)
                        digest.update(chunk)
                    require(actual == size and digest.hexdigest() == sha256, "gc_archive_invalid")
                    output.flush()
                    os.fsync(output.fileno())
                # Rename publication is atomic and never replaces an existing
                # name. A crash cannot leave a hard-linked stage beside target.
                target = self._path(key)
                self.checkpoint()
                try:
                    _publish_noreplace(stage, target)
                except FileExistsError:
                    return self._existing(key, size, sha256)
                created = None
                _fsync_directory(target.parent)
                return {"backend": "local", "status": "restored", "key": key}
        except BaseException as error:
            if isinstance(error, FileGCError) or not isinstance(error, Exception):
                raise
            raise FileGCError("gc_restore_failed") from None
        finally:
            if created is not None:
                _remove_created(created)

    def _existing(self, key, size, sha256):
        record = self.head(key)
        require(record.size == size, "gc_restore_conflict")
        with _read_open(self._path(key)) as stream:
            _same(record, _local_record(key, os.fstat(stream.fileno())))
            _matches(iter(lambda: stream.read(CHUNK_BYTES), b""), size, sha256, self.checkpoint)
            _same(record, _local_record(key, os.fstat(stream.fileno())))
        _same(record, self.head(key))
        # A previous process may have exited after rename but before directory
        # fsync. Complete that durable publication when retrying an exact match.
        _fsync_directory(self._path(key).parent)
        return {"backend": "local", "status": "already_present", "key": key}


class OSSGCStorage:
    def __init__(self, cfg, client=None):
        self.checkpoint = _noop
        try:
            import alibabacloud_oss_v2 as oss
            self.oss = oss
            self.bucket, self.prefix, self.region = cfg.oss_bucket, cfg.oss_prefix, cfg.oss_region
            require(isinstance(self.bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket)
                    and isinstance(self.region, str) and re.fullmatch(r"[a-z0-9-]{1,63}", self.region)
                    and isinstance(self.prefix, str)
                    and re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*/", self.prefix),
                    "gc_storage_config_invalid")
            if client is not None:
                self.client = client
                return
            sdk = oss.config.load_default()
            sdk.region, sdk.endpoint = self.region, cfg.oss_endpoint or f"https://oss-{self.region}.aliyuncs.com"
            require(sdk.endpoint.startswith("https://"), "gc_storage_config_invalid")
            sdk.signature_version = "v4"
            sdk.credentials_provider = oss.credentials.StaticCredentialsProvider(
                cfg.oss_access_key_id.get_secret_value(), cfg.oss_access_key_secret.get_secret_value(),
                cfg.oss_security_token.get_secret_value() or None)
            sdk.connect_timeout, sdk.readwrite_timeout = 5, 20
            sdk.retry_max_attempts = 0  # SDK clamps zero to one initial attempt, with no retries.
            sdk.enabled_redirect, sdk.insecure_skip_verify = False, False
            self.client = oss.Client(sdk)
        except FileGCError:
            raise
        except Exception:
            raise FileGCError("gc_storage_config_invalid") from None

    def backend_identity(self):
        return {"backend": "oss", "bucket": self.bucket, "prefix": self.prefix, "region": self.region}

    def _call(self, method, request, *, write=False):
        from alibabacloud_oss_v2.exceptions import OperationError, ServiceError
        self.checkpoint()
        try:
            result = getattr(self.client, method)(request, **({"retry_max_attempts": 0} if write else {}))
            # Preserve a successful destructive response for the caller's durable
            # receipt. Read operations can safely be interrupted after returning.
            if not write:
                try:
                    self.checkpoint()
                except BaseException:
                    body = getattr(result, "body", None)
                    if body is not None:
                        try:
                            body.close()
                        except Exception:
                            pass
                    raise
            return result
        except FileGCError:
            raise
        except Exception as error:
            for _ in range(8):
                if isinstance(error, ServiceError):
                    if not write and error.status_code == 404 and error.code in {"NoSuchKey", "NoSuchVersion"}:
                        raise FileNotFoundError("object_not_found") from None
                    break
                if not isinstance(error, OperationError):
                    break
                error = error.unwrap()
            raise FileGCError("gc_delete_unknown" if write else "gc_storage_unavailable") from None

    def _record(self, key, result):
        size = getattr(result, "content_length", getattr(result, "size", None))
        etag, version = getattr(result, "etag", None), getattr(result, "version_id", None)
        require(type(size) is int and size >= 0 and _text(etag, 512)
                and (version is None or _text(version)), "gc_storage_metadata_invalid")
        return ObjectRecord(key, size, _iso(result.last_modified), {"etag": etag, "version_id": version})

    def inventory(self, limit=MAX_OBJECTS):
        self.checkpoint()
        require(type(limit) is int and 0 < limit <= MAX_OBJECTS, "gc_inventory_limit")
        result, seen, tokens, token = [], set(), set(), None
        try:
            while True:
                self.checkpoint()
                page = self._call("list_objects_v2", self.oss.ListObjectsV2Request(
                    bucket=self.bucket, prefix=self.prefix, max_keys=min(1000, limit), continuation_token=token))
                require(type(page.is_truncated) is bool and not getattr(page, "common_prefixes", None),
                        "gc_inventory_incomplete")
                for item in page.contents or []:
                    self.checkpoint()
                    require(isinstance(item.key, str) and item.key.startswith(self.prefix)
                            and item.key not in seen, "gc_inventory_incomplete")
                    seen.add(item.key)
                    key = item.key[len(self.prefix):]
                    listed = self._record(key, item)
                    # Unknown names are inventoried as returned; no operation is issued on them.
                    try:
                        _key(key)
                    except FileGCError:
                        safe = False
                    else:
                        safe = True
                    if safe:
                        record = self.head(key)
                        require((record.size, record.modified_at, record.identity["etag"])
                                == (listed.size, listed.modified_at, listed.identity["etag"]), "gc_object_changed")
                    else:
                        record = listed
                    result.append(record)
                    require(len(result) <= limit, "gc_inventory_limit")
                if not page.is_truncated:
                    return sorted(result, key=lambda item: item.key)
                token = page.next_continuation_token
                require(_text(token, 4096) and token not in tokens and len(tokens) < MAX_OBJECTS,
                        "gc_inventory_incomplete")
                tokens.add(token)
        except FileGCError:
            raise
        except Exception:
            raise FileGCError("gc_inventory_incomplete") from None

    def head(self, key):
        result = self._call("head_object", self.oss.HeadObjectRequest(bucket=self.bucket, key=self.prefix + _key(key)))
        try:
            return self._record(key, result)
        except FileGCError:
            raise
        except Exception:
            raise FileGCError("gc_storage_metadata_invalid") from None

    def _version_record(self, record):
        version = record.identity.get("version_id")
        require(_text(version), "gc_storage_metadata_invalid")
        result = self._call("head_object", self.oss.HeadObjectRequest(
            bucket=self.bucket, key=self.prefix + _key(record.key), version_id=version))
        current = self._record(record.key, result)
        _same(record, current)

    def _chunks(self, record):
        result = self._call("get_object", self.oss.GetObjectRequest(
            bucket=self.bucket, key=self.prefix + _key(record.key),
            version_id=record.identity.get("version_id"),
            if_match=record.identity["etag"] if record.identity.get("version_id") is None else None))
        try:
            _same(record, self._record(record.key, result))
            for chunk in result.body.iter_bytes(block_size=CHUNK_BYTES):
                self.checkpoint()
                yield chunk
            self.checkpoint()
        finally:
            result.body.close()

    def export(self, record, destination):
        self.checkpoint()
        copied = None
        try:
            _same(record, self.head(record.key))
            chunks = self._chunks(record)
            try:
                copied = _capture(chunks, destination, record.size, self.checkpoint)
            finally:
                chunks.close()
            _same(record, self.head(record.key))
            return copied
        except BaseException as error:
            if copied is not None:
                _remove_created(Path(destination))
            if isinstance(error, (FileGCError, FileNotFoundError)) or not isinstance(error, Exception):
                raise
            raise FileGCError("gc_export_failed") from None

    def ensure_writable(self):
        result = self._call("get_bucket_versioning", self.oss.GetBucketVersioningRequest(bucket=self.bucket))
        require(getattr(result, "version_status", None) == "Enabled", "gc_oss_versioning_required")

    def delete(self, record):
        self.checkpoint()
        self.ensure_writable()
        _same(record, self.head(record.key))
        require(_text(record.identity.get("version_id")), "gc_storage_metadata_invalid")
        result = self._call("delete_object", self.oss.DeleteObjectRequest(
            bucket=self.bucket, key=self.prefix + _key(record.key)), write=True)
        marker = getattr(result, "version_id", None)
        require(getattr(result, "delete_marker", None) is True and _text(marker) and marker != "null"
                and marker != record.identity["version_id"], "gc_delete_unknown")
        return {"backend": "oss", "status": "deleted", "key": record.key, "delete_marker": True,
                "marker_version_id": marker, "original_version_id": record.identity["version_id"]}

    def _versions(self, key):
        scoped, versions, markers, cursors, cursor, count = self.prefix + _key(key), [], [], set(), (None, None), 0
        while True:
            self.checkpoint()
            page = self._call("list_object_versions", self.oss.ListObjectVersionsRequest(
                bucket=self.bucket, prefix=scoped, max_keys=999,
                key_marker=cursor[0], version_id_marker=cursor[1]))
            require(type(page.is_truncated) is bool and not getattr(page, "common_prefixes", None),
                    "gc_restore_versions_invalid")
            for collection, output in ((page.version or [], versions), (page.delete_marker or [], markers)):
                for item in collection:
                    self.checkpoint()
                    count += 1
                    require(count <= MAX_OBJECTS and isinstance(item.key, str) and item.key.startswith(scoped),
                            "gc_restore_versions_invalid")
                    if item.key == scoped:
                        require(_text(item.version_id) and type(item.is_latest) is bool,
                                "gc_restore_versions_invalid")
                        output.append(item)
            if not page.is_truncated:
                return versions, markers
            cursor = (page.next_key_marker, page.next_version_id_marker)
            require(_text(cursor[0]) and (cursor[1] is None or _text(cursor[1])) and cursor not in cursors
                    and len(cursors) < MAX_OBJECTS,
                    "gc_restore_versions_invalid")
            cursors.add(cursor)

    def restore(self, key, payload, *, size, sha256, original=None, receipt=None):
        # No PUT: forbid_overwrite is ignored in version-enabled buckets. Removing
        # only our proven delete marker cannot overwrite a concurrent new version.
        try:
            self.checkpoint()
            _key(key)
            with _payload(payload, size, sha256, self.checkpoint):
                pass
            if not (isinstance(original, ObjectRecord) and original.key == key and original.size == size
                    and isinstance(receipt, dict) and receipt.get("backend") == "oss"
                    and receipt.get("status") == "deleted" and receipt.get("key") == key
                    and receipt.get("delete_marker") is True
                    and _text(receipt.get("marker_version_id")) and receipt["marker_version_id"] != "null"
                    and receipt.get("original_version_id") == original.identity.get("version_id")):
                return {"backend": "oss", "status": "manual_required", "key": key}
            self.ensure_writable()
            try:
                self.head(key)
            except FileNotFoundError:
                pass
            else:
                current = self.head(key)
                require(current.size == size, "gc_restore_conflict")
                chunks = self._chunks(current)
                try:
                    _matches(chunks, size, sha256, self.checkpoint)
                finally:
                    chunks.close()
                _same(current, self.head(key))
                return {"backend": "oss", "status": "already_present", "key": key}
            self._version_record(original)
            digest, actual = hashlib.sha256(), 0
            chunks = self._chunks(original)
            try:
                for chunk in chunks:
                    self.checkpoint()
                    actual += len(chunk)
                    require(actual <= size, "gc_object_changed")
                    digest.update(chunk)
            finally:
                chunks.close()
            require(actual == size and digest.hexdigest() == sha256, "gc_archive_invalid")
            versions, markers = self._versions(key)
            original_id, marker_id = original.identity["version_id"], receipt["marker_version_id"]
            require(versions and versions[0].version_id == original_id
                    and len({v.version_id for v in versions}) == len(versions)
                    and not any(v.is_latest for v in versions), "gc_restore_versions_changed")
            selected = [m for m in markers if m.version_id == marker_id]
            require(len(selected) == 1 and selected[0].is_latest
                    and len([m for m in markers if m.is_latest]) == 1
                    and not any(v.version_id == marker_id for v in versions), "gc_restore_marker_changed")
            require(all(m.last_modified.astimezone(timezone.utc) < datetime.fromisoformat(original.modified_at)
                        for m in markers if m.version_id != marker_id),
                    "gc_restore_versions_changed")
            removed = self._call("delete_object", self.oss.DeleteObjectRequest(
                bucket=self.bucket, key=self.prefix + key, version_id=marker_id), write=True)
            require(getattr(removed, "delete_marker", None) is True
                    and getattr(removed, "version_id", None) == marker_id, "gc_delete_unknown")
            return {"backend": "oss", "status": "restored", "key": key, "removed_marker_version_id": marker_id}
        except FileGCError:
            raise
        except FileNotFoundError:
            raise FileGCError("gc_restore_original_missing") from None
        except Exception:
            raise FileGCError("gc_restore_failed") from None
