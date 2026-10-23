"""Bounded operator-only PostgreSQL and object readers for site backups.

No command reads an application secret into its arguments or diagnostic output.
The caller selects the database, maintenance window, and private destinations.
"""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import threading


CHUNK_BYTES = 256 * 1024
_WINDOWS = os.name == "nt"


class SiteBackupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise SiteBackupError(code) from None


def _no_aliases(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "site_storage_invalid")


def _private_output(path):
    try:
        path = Path(path).absolute()
        _no_aliases(path)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        return path, os.fdopen(descriptor, "wb")
    except SiteBackupError:
        raise
    except (OSError, TypeError, ValueError):
        raise SiteBackupError("site_storage_unavailable") from None


def _capture(chunks, destination, max_bytes, expected_size):
    _require(type(max_bytes) is int and max_bytes > 0, "site_object_size_exceeded")
    _require(type(expected_size) is int and 0 <= expected_size <= max_bytes,
             "site_object_size_exceeded")
    created, size, digest = None, 0, hashlib.sha256()
    try:
        created, stream = _private_output(destination)
        with stream:
            for chunk in chunks:
                _require(isinstance(chunk, bytes), "site_storage_unavailable")
                size += len(chunk)
                _require(size <= max_bytes and size <= expected_size, "site_object_changed")
                stream.write(chunk)
                digest.update(chunk)
            _require(size == expected_size, "site_object_changed")
            stream.flush()
            os.fsync(stream.fileno())
        return {"size": size, "sha256": digest.hexdigest(), "version_id": None, "etag": None}
    except BaseException:
        if created is not None:
            created.unlink(missing_ok=True)
        raise


class LocalBackupStorage:
    def __init__(self, root):
        try:
            self.root = Path(root).absolute()
            _no_aliases(self.root)
            _require(self.root.is_dir(), "site_storage_invalid")
            self.root = self.root.resolve()
        except SiteBackupError:
            raise
        except (OSError, TypeError, ValueError):
            raise SiteBackupError("site_storage_invalid") from None

    def _path(self, key):
        _require(isinstance(key, str)
                 and re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+", key)
                 and all(part not in {".", ".."} for part in key.split("/")),
                 "site_storage_invalid")
        path = self.root.joinpath(*key.split("/"))
        _no_aliases(path)
        _require(path.resolve().is_relative_to(self.root), "site_storage_invalid")
        return path

    def capture(self, key, destination, max_bytes):
        result, opened = None, False
        try:
            path = self._path(key)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                opened = True
                before = os.fstat(source.fileno())
                _require(stat.S_ISREG(before.st_mode), "site_storage_invalid")
                chunks = iter(lambda: source.read(CHUNK_BYTES), b"")
                result = _capture(chunks, destination, max_bytes, before.st_size)
                after = os.fstat(source.fileno())
                self._path(key)
                current = path.stat()
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                # Windows fstat/path.stat disagree on ctime semantics. Compare
                # it within the open descriptor, and use identity/size/mtime
                # to verify the pathname still identifies that same object.
                path_fields = fields[:-1] if _WINDOWS else fields
                _require(all(getattr(before, field) == getattr(after, field) for field in fields)
                         and all(getattr(after, field) == getattr(current, field) for field in path_fields),
                         "site_object_changed")
            return result
        except FileNotFoundError:
            if result is not None:
                Path(destination).unlink(missing_ok=True)
            if opened:
                raise SiteBackupError("site_object_changed") from None
            raise FileNotFoundError("object_not_found") from None
        except BaseException as error:
            if result is not None:
                Path(destination).unlink(missing_ok=True)
            if isinstance(error, SiteBackupError):
                raise
            if not isinstance(error, Exception):
                raise
            raise SiteBackupError("site_storage_unavailable") from None


class OSSBackupStorage:
    def __init__(self, cfg, client=None):
        # Reuse the application's HTTPS/V4, fixed-bucket credential setup.
        from app.modules.files.oss_storage import OSSStorage
        try:
            self.storage = OSSStorage(cfg, client=client)
        except Exception:
            raise SiteBackupError("site_storage_unavailable") from None

    def _call(self, method, request):
        from alibabacloud_oss_v2.exceptions import OperationError, ServiceError
        try:
            return getattr(self.storage.client, method)(request)
        except Exception as error:
            for _ in range(8):
                if isinstance(error, ServiceError):
                    if error.status_code == 404 and error.code in {"NoSuchKey", "NoSuchVersion"}:
                        raise FileNotFoundError("object_not_found") from None
                    break
                if not isinstance(error, OperationError):
                    break
                error = error.unwrap()
            raise SiteBackupError("site_storage_unavailable") from None

    def capture(self, key, destination, max_bytes):
        import alibabacloud_oss_v2 as oss
        result, copied = None, None
        try:
            scoped_key = self.storage._key(key)
            head = self._call("head_object", oss.HeadObjectRequest(
                bucket=self.storage.bucket, key=scoped_key))
            size, version, etag = head.content_length, getattr(head, "version_id", None), getattr(head, "etag", None)
            _require(type(max_bytes) is int and max_bytes > 0
                     and type(size) is int and 0 <= size <= max_bytes, "site_object_size_exceeded")
            _require(version is None or isinstance(version, str) and 0 < len(version) <= 1024
                     and not any(ord(char) < 32 for char in version), "site_storage_unavailable")
            _require(isinstance(etag, str) and 0 < len(etag) <= 512
                     and not any(ord(char) < 32 for char in etag), "site_storage_unavailable")
            try:
                result = self._call("get_object", oss.GetObjectRequest(
                    bucket=self.storage.bucket, key=scoped_key,
                    version_id=version, if_match=etag if version is None else None))
            except FileNotFoundError:
                # HEAD found this exact object. A later deletion is a failed
                # consistent backup, including for otherwise optional objects.
                raise SiteBackupError("site_object_changed") from None
            _require(type(result.content_length) is int and result.content_length == size
                     and getattr(result, "version_id", None) == version
                     and getattr(result, "etag", None) == etag, "site_object_changed")
            copied = _capture(result.body.iter_bytes(block_size=CHUNK_BYTES), destination, max_bytes, size)
            copied.update(version_id=version, etag=etag)
            return copied
        except (FileNotFoundError, SiteBackupError):
            raise
        except Exception:
            raise SiteBackupError("site_storage_unavailable") from None
        finally:
            if result is not None:
                try:
                    result.body.close()
                except Exception:
                    if copied is not None:
                        Path(destination).unlink(missing_ok=True)
                    raise SiteBackupError("site_storage_unavailable") from None


class DockerPostgres:
    """Use the explicitly selected local container's Unix socket, never passwords."""

    def __init__(self, engine, container, *, timeout=300):
        self.engine, self.container, self.timeout = engine, container, timeout
        _require(isinstance(container, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container),
                 "site_postgres_invalid")
        _require(isinstance(timeout, (int, float)) and 0 < timeout <= 3600, "site_postgres_invalid")
        url = engine.url
        _require(url.drivername == "postgresql+psycopg" and bool(url.username) and bool(url.database)
                 and all("\x00" not in value for value in (url.username, url.database)), "site_postgres_invalid")
        self.username, self.database = url.username, url.database

    def _command(self, binary, *arguments):
        return ["docker", "exec", "-i", self.container, "env", "-u", "PGSERVICE", "-u", "PGOPTIONS",
                "-u", "PGSERVICEFILE", "-u", "PGPASSFILE", "-u", "PGPASSWORD", binary,
                "--host=/var/run/postgresql", "--port=5432",
                f"--username={self.username}", f"--dbname={self.database}", *arguments]

    def _run(self, command, *, destination=None, source=None, max_bytes=64 * 1024):
        process, timer, created = None, None, None
        expired, chunks, size = threading.Event(), [], 0
        try:
            with ExitStack() as stack:
                incoming = stack.enter_context(Path(source).open("rb")) if source is not None else subprocess.DEVNULL
                outgoing = None
                if destination is not None:
                    created, outgoing = _private_output(destination)
                    stack.enter_context(outgoing)
                process = subprocess.Popen(command, stdin=incoming, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)

                def timeout():
                    expired.set()
                    try:
                        process.kill()
                    except OSError:
                        pass

                timer = threading.Timer(self.timeout, timeout)
                timer.daemon = True
                timer.start()
                while chunk := process.stdout.read(CHUNK_BYTES):
                    size += len(chunk)
                    _require(size <= max_bytes, "site_postgres_size_exceeded")
                    if outgoing is None:
                        chunks.append(chunk)
                    else:
                        outgoing.write(chunk)
                code = process.wait(timeout=self.timeout)
                _require(not expired.is_set(), "site_postgres_timeout")
                _require(code == 0, "site_postgres_failed")
                if outgoing is not None:
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                return b"".join(chunks)
        except BaseException as error:
            if created is not None:
                created.unlink(missing_ok=True)
            if isinstance(error, SiteBackupError):
                raise
            if not isinstance(error, Exception):
                raise
            raise SiteBackupError("site_postgres_failed") from None
        finally:
            if timer is not None:
                timer.cancel()
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                process.stdout.close()

    def check(self):
        from sqlalchemy import text
        query = ("SELECT current_database(), current_setting('server_version_num'), "
                 "system_identifier::text FROM pg_catalog.pg_control_system()")
        try:
            with self.engine.connect() as connection:
                actual = tuple(str(value) for value in connection.execute(text(query)).one())
            output = self._run(self._command("psql", "--no-psqlrc", "--tuples-only", "--no-align",
                                             "--field-separator=|", f"--command={query}"), max_bytes=1024)
            inside = tuple(output.decode("ascii").strip().split("|"))
            _require(actual == inside and len(actual) == 3 and actual[0] == self.database
                     and actual[1].isdigit() and int(actual[1]) // 10000 == 16
                     and actual[2].isdigit(), "site_postgres_mismatch")
            for binary in ("pg_dump", "pg_restore"):
                output = self._run(["docker", "exec", self.container, binary, "--version"], max_bytes=1024)
                _require(re.fullmatch(rb"pg_(?:dump|restore) \(PostgreSQL\) 16(?:\.[^\r\n]+)?\r?\n?", output)
                         is not None, "site_postgres_version_invalid")
            return {"pg_major": 16, "system_identifier": actual[2]}
        except SiteBackupError:
            raise
        except Exception:
            raise SiteBackupError("site_postgres_failed") from None

    def dump(self, snapshot, path, max_bytes):
        _require(isinstance(snapshot, str) and re.fullmatch(r"[A-Fa-f0-9]+-[A-Fa-f0-9]+-[0-9]+", snapshot),
                 "site_postgres_snapshot_invalid")
        _require(type(max_bytes) is int and max_bytes > 0, "site_postgres_size_exceeded")
        self._run(self._command("pg_dump", "--format=custom", "--no-owner", "--no-privileges",
                                f"--snapshot={snapshot}"), destination=path, max_bytes=max_bytes)

    def restore(self, path):
        try:
            path = Path(path).absolute()
            _no_aliases(path)
            _require(path.is_file(), "site_postgres_invalid")
            self._run(self._command("pg_restore", "--single-transaction", "--no-owner", "--no-privileges",
                                    "--exit-on-error"), source=path)
        except SiteBackupError:
            raise
        except Exception:
            raise SiteBackupError("site_postgres_failed") from None
