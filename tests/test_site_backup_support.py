"""Operator backup adapters: generated local files and mocked services only."""

import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("site_backup_support", ROOT / "scripts/site_backup_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
HAS_BACKEND = importlib.util.find_spec("sqlalchemy") is not None
HAS_OSS = importlib.util.find_spec("alibabacloud_oss_v2") is not None


class LocalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        (self.storage / "owner").mkdir()
        self.source = self.storage / "owner" / "original"
        self.source.write_bytes(b"private fixture bytes")
        self.target = self.root / "captured"
        self.store = support.LocalBackupStorage(self.storage)

    def assert_code(self, code, function, *args):
        with self.assertRaises(support.SiteBackupError) as caught:
            function(*args)
        self.assertEqual(str(caught.exception), code)

    def test_bounded_binary_copy_has_digest_and_private_mode(self):
        result = self.store.capture("owner/original", self.target, 100)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(result, {"size": 21, "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                                  "version_id": None, "etag": None})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_empty_object_is_valid(self):
        self.source.write_bytes(b"")
        self.assertEqual(self.store.capture("owner/original", self.target, 1)["size"], 0)

    def test_missing_object_uses_only_fixed_message(self):
        with self.assertRaises(FileNotFoundError) as caught:
            self.store.capture("owner/missing", self.target, 100)
        self.assertEqual(str(caught.exception), "object_not_found")
        self.assertFalse(self.target.exists())

    def test_existing_destination_is_never_overwritten_or_deleted(self):
        self.target.write_bytes(b"existing backup")
        self.assert_code("site_storage_unavailable", self.store.capture, "owner/original", self.target, 100)
        self.assertEqual(self.target.read_bytes(), b"existing backup")

    def test_missing_output_parent_is_not_reported_as_missing_object(self):
        self.assert_code("site_storage_unavailable", self.store.capture, "owner/original",
                         self.root / "absent" / "captured", 100)

    def test_size_limit_does_not_publish_output(self):
        self.assert_code("site_object_size_exceeded", self.store.capture, "owner/original", self.target, 3)
        self.assertFalse(self.target.exists())

    def test_traversal_and_invalid_keys_are_rejected(self):
        for key in ("owner/../secret", "/owner/original", "owner\\original", "owner/./original",
                    "owner", "https://host/key", None):
            with self.subTest(key=key):
                self.assert_code("site_storage_invalid", self.store.capture, key, self.target, 100)

    def test_alias_is_rejected_before_read(self):
        try:
            (self.storage / "link").symlink_to(self.storage / "owner", target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires OS privileges")
        self.assert_code("site_storage_invalid", self.store.capture, "link/original", self.target, 100)

    def test_windows_reparse_attribute_is_rejected(self):
        original = Path.lstat

        def fake_lstat(path):
            if path == self.source:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(path)

        with patch.object(Path, "lstat", fake_lstat):
            self.assert_code("site_storage_invalid", self.store.capture, "owner/original", self.target, 100)

    def test_mutation_after_copy_removes_just_created_output(self):
        before = self.source.stat()
        fields = ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        changed = SimpleNamespace(**{field: getattr(before, field) for field in fields})
        changed.st_mtime_ns += 1
        with patch.object(support.os, "fstat", side_effect=[before, changed]):
            self.assert_code("site_object_changed", self.store.capture, "owner/original", self.target, 100)
        self.assertFalse(self.target.exists())

    def test_windows_path_stat_ctime_difference_does_not_look_like_mutation(self):
        before = self.source.stat()
        fields = ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        descriptor = SimpleNamespace(**{field: getattr(before, field) for field in fields})
        descriptor.st_ctime_ns += 1
        with patch.object(support, "_WINDOWS", True), patch.object(support.os, "fstat", return_value=descriptor):
            self.assertEqual(self.store.capture("owner/original", self.target, 100)["size"], 21)

    def test_stream_failure_or_short_read_removes_partial_output(self):
        def broken():
            yield b"x"
            raise OSError("secret-path")

        for chunks in (iter([b"x"]), broken(), iter([b"longer"])):
            with self.subTest(chunks=chunks):
                with self.assertRaises((support.SiteBackupError, OSError)):
                    support._capture(chunks, self.target, 100, 3)
                self.assertFalse(self.target.exists())


class Body:
    def __init__(self, chunks=(b"fi", b"xture"), failure=False):
        self.chunks, self.failure, self.closed = chunks, failure, False

    def iter_bytes(self, **kwargs):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise OSError("signed-url-secret")

    def close(self):
        self.closed = True


@unittest.skipUnless(HAS_OSS, "OSS SDK is installed with backend dependencies")
class OSSCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "captured"
        self.client = Mock()
        self.body = Body()
        self.cfg = SimpleNamespace(oss_bucket="fixture-bucket", oss_prefix="private/",
                                   upload_max_bytes=100, pdf_max_bytes=100)
        self.store = support.OSSBackupStorage(self.cfg, client=self.client)
        self.response()

    def response(self, version="version-1", etag='"fixture-etag"', **get_changes):
        self.client.head_object.return_value = SimpleNamespace(content_length=7, version_id=version, etag=etag)
        self.client.get_object.return_value = SimpleNamespace(
            **({"content_length": 7, "version_id": version, "etag": etag,
                "body": self.body} | get_changes))

    def test_selected_version_is_recorded_and_requested(self):
        result = self.store.capture("owner/original", self.target, 100)
        request = self.client.get_object.call_args.args[0]
        self.assertEqual((request.bucket, request.key, request.version_id, request.if_match),
                         ("fixture-bucket", "private/owner/original", "version-1", None))
        self.assertEqual(result["version_id"], "version-1")
        self.assertEqual(result["etag"], '"fixture-etag"')
        self.assertEqual(result["sha256"], hashlib.sha256(b"fixture").hexdigest())
        self.assertTrue(self.body.closed)

    def test_null_version_is_explicit_and_not_treated_as_unversioned(self):
        self.response(version="null")
        self.store.capture("owner/original", self.target, 100)
        request = self.client.get_object.call_args.args[0]
        self.assertEqual(request.version_id, "null")
        self.assertIsNone(request.if_match)

    def test_unversioned_read_requires_matching_etag(self):
        self.response(version=None)
        self.store.capture("owner/original", self.target, 100)
        request = self.client.get_object.call_args.args[0]
        self.assertIsNone(request.version_id)
        self.assertEqual(request.if_match, '"fixture-etag"')

    def test_changed_get_metadata_closes_body_and_rejects(self):
        for changes in ({"content_length": 8}, {"version_id": "version-2"}, {"etag": "changed"}):
            with self.subTest(changes=changes):
                self.body = Body()
                self.response()
                for key, value in changes.items():
                    setattr(self.client.get_object.return_value, key, value)
                with self.assertRaisesRegex(support.SiteBackupError, "^site_object_changed$"):
                    self.store.capture("owner/original", self.target, 100)
                self.assertTrue(self.body.closed)
                self.assertFalse(self.target.exists())

    def test_disappearance_after_successful_head_fails_consistent_backup(self):
        from alibabacloud_oss_v2.exceptions import ServiceError
        self.client.get_object.side_effect = ServiceError(
            status_code=404, code="NoSuchVersion", request_id="test", message="removed",
            ec="test", timestamp="test", request_target="https://secret")
        with self.assertRaisesRegex(support.SiteBackupError, "^site_object_changed$"):
            self.store.capture("owner/original", self.target, 100)
        self.assertFalse(self.target.exists())

    def test_size_guard_prevents_get_request(self):
        with self.assertRaisesRegex(support.SiteBackupError, "^site_object_size_exceeded$"):
            self.store.capture("owner/original", self.target, 3)
        self.client.get_object.assert_not_called()

    def test_short_long_and_failed_streams_remove_output_and_close(self):
        for body in (Body([b"short"]), Body([b"far-too-long"]), Body(failure=True)):
            with self.subTest(body=body):
                self.body = body
                self.response()
                with self.assertRaises(support.SiteBackupError):
                    self.store.capture("owner/original", self.target, 100)
                self.assertTrue(body.closed)
                self.assertFalse(self.target.exists())

    def test_only_missing_key_and_version_404_become_file_not_found(self):
        from alibabacloud_oss_v2.exceptions import OperationError, ServiceError
        for status, code in ((404, "NoSuchKey"), (404, "NoSuchVersion"), (403, "AccessDenied"),
                             (404, "AccessDenied"), (412, "PreconditionFailed")):
            with self.subTest(code=code, status=status):
                self.client.head_object.side_effect = OperationError(name="HeadObject", error=ServiceError(
                    status_code=status, code=code, request_id="test", message="secret-token",
                    ec="test", timestamp="test", request_target="https://signed-secret"))
                expected = FileNotFoundError if status == 404 and code.startswith("NoSuch") else support.SiteBackupError
                with self.assertRaises(expected) as caught:
                    self.store.capture("owner/original", self.target, 100)
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_unexpected_sdk_exception_is_redacted(self):
        self.client.get_object.side_effect = OSError("signed-secret-url")
        with self.assertRaisesRegex(support.SiteBackupError, "^site_storage_unavailable$") as caught:
            self.store.capture("owner/original", self.target, 100)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_stream_close_failure_removes_completed_output(self):
        self.body.close = Mock(side_effect=OSError("private-response"))
        with self.assertRaisesRegex(support.SiteBackupError, "^site_storage_unavailable$"):
            self.store.capture("owner/original", self.target, 100)
        self.assertFalse(self.target.exists())

    def test_missing_etag_is_not_allowed_for_conditional_capture(self):
        self.response(version=None, etag=None)
        with self.assertRaisesRegex(support.SiteBackupError, "^site_storage_unavailable$"):
            self.store.capture("owner/original", self.target, 100)
        self.client.get_object.assert_not_called()


def fake_engine(database="notes_restore_site_test"):
    return SimpleNamespace(url=SimpleNamespace(drivername="postgresql+psycopg", username="notes",
                                               password="secret-password", database=database))


class DockerPostgresTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "database.dump"
        self.runner = support.DockerPostgres(fake_engine(), "fixture-db", timeout=5)

    def test_commands_have_explicit_socket_database_and_no_password(self):
        with patch.object(self.runner, "_run") as run:
            self.runner.dump("00000001-00000002-1", self.target, 1024)
        command = run.call_args.args[0]
        self.assertIn("--host=/var/run/postgresql", command)
        self.assertIn("--port=5432", command)
        self.assertIn("--username=notes", command)
        self.assertIn("--dbname=notes_restore_site_test", command)
        self.assertIn("--snapshot=00000001-00000002-1", command)
        self.assertNotIn("secret-password", repr(command))

    def test_restore_is_one_transaction_and_does_not_clean_or_create(self):
        self.target.write_bytes(b"PGDMPfixture")
        with patch.object(self.runner, "_run") as run:
            self.runner.restore(self.target)
        command = run.call_args.args[0]
        for flag in ("--single-transaction", "--no-owner", "--no-privileges", "--exit-on-error"):
            self.assertIn(flag, command)
        self.assertNotIn("--clean", command)
        self.assertNotIn("--create", command)
        self.assertEqual(run.call_args.kwargs["source"], self.target)

    def test_bad_container_and_snapshot_rejected_before_execution(self):
        for name in ("--privileged", "name;whoami", "bad name"):
            with self.assertRaises(support.SiteBackupError):
                support.DockerPostgres(fake_engine(), name)
        with patch.object(self.runner, "_run") as run:
            with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_snapshot_invalid$"):
                self.runner.dump("invalid --command=secret", self.target, 100)
            run.assert_not_called()

    def test_binary_stream_is_bounded_and_partial_output_removed(self):
        command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*300000)"]
        with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_size_exceeded$"):
            self.runner._run(command, destination=self.target, max_bytes=100)
        self.assertFalse(self.target.exists())

    def test_nonzero_exit_stderr_is_redacted(self):
        command = [sys.executable, "-c", "import sys;sys.stderr.write('secret-token');sys.exit(3)"]
        with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_failed$") as caught:
            self.runner._run(command, destination=self.target)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertFalse(self.target.exists())

    def test_hung_process_times_out_and_removes_output(self):
        self.runner.timeout = 0.2
        command = [sys.executable, "-c", "import time;time.sleep(5)"]
        with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_timeout$"):
            self.runner._run(command, destination=self.target)
        self.assertFalse(self.target.exists())

    def test_stream_does_not_overwrite_existing_destination(self):
        self.target.write_bytes(b"existing")
        with self.assertRaises(support.SiteBackupError):
            self.runner._run([sys.executable, "-c", "pass"], destination=self.target)
        self.assertEqual(self.target.read_bytes(), b"existing")

    @unittest.skipUnless(HAS_BACKEND, "SQLAlchemy is installed with backend dependencies")
    def test_cluster_identity_database_and_version_must_match(self):
        connection = Mock()
        connection.execute.return_value.one.return_value = ("notes_restore_site_test", "160008", "12345")
        engine = fake_engine()
        engine.connect = Mock()
        engine.connect.return_value.__enter__ = Mock(return_value=connection)
        engine.connect.return_value.__exit__ = Mock(return_value=None)
        runner = support.DockerPostgres(engine, "fixture-db")
        for response in (b"notes_restore_site_test|160008|99999\n", b"other|160008|12345\n",
                         b"notes_restore_site_test|150008|12345\n"):
            with patch.object(runner, "_run", return_value=response):
                with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_mismatch$"):
                    runner.check()
        with patch.object(runner, "_run", side_effect=[b"notes_restore_site_test|160008|12345\n",
                                                       b"pg_dump (PostgreSQL) 16.8\n",
                                                       b"pg_restore (PostgreSQL) 16.8\n"]):
            self.assertEqual(runner.check(), {"pg_major": 16, "system_identifier": "12345"})
        with patch.object(runner, "_run", side_effect=[b"notes_restore_site_test|160008|12345\n",
                                                       b"pg_dump (PostgreSQL) 17.0\n"]):
            with self.assertRaisesRegex(support.SiteBackupError, "^site_postgres_version_invalid$"):
                runner.check()


if __name__ == "__main__":
    unittest.main()
