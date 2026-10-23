from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alibabacloud_oss_v2.exceptions import OperationError, ServiceError
from pydantic import ValidationError

from app.modules.files.oss_storage import OSSStorage, StorageUnavailable
from app.shared.config import Settings


@pytest.fixture(autouse=True)
def clean_database():
    yield


def configured(**changes):
    return Settings(_env_file=None, storage_backend="oss", oss_bucket="example-bucket",
                    oss_access_key_id="dummy-access-id", oss_access_key_secret="dummy-test-secret",
                    **changes)


class Body:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_bytes(self, **_):
        yield from self.chunks


def test_sdk_operations_are_scoped_and_server_side():
    client = Mock()
    store = OSSStorage(configured(), client=client)
    key = "user/file/original"
    store.put(key, b"image")
    request = client.put_object.call_args.args[0]
    assert request.bucket == "example-bucket"
    assert request.key == "ai-notes/user/file/original"
    assert request.body == b"image" and request.content_length == 5
    client.head_object.return_value = SimpleNamespace(content_length=5)
    assert store.head(key).size == 5
    body = Body([b"im", b"age"])
    client.get_object.return_value = SimpleNamespace(content_length=5, body=body,
                                                     content_disposition="attachment")
    assert store.read(key) == b"image" and body.closed
    store.delete(key)
    request = client.delete_object.call_args.args[0]
    assert request.key == "ai-notes/user/file/original"
    assert request.version_id is None  # Keep recoverable historical versions.


def test_sdk_missing_key_and_errors_are_redacted():
    client = Mock()
    store = OSSStorage(configured(), client=client)
    missing = ServiceError(status_code=404, code="NoSuchKey", request_id="test-request",
                           message="missing", ec="test", timestamp="test", request_target="test")
    client.head_object.side_effect = OperationError(name="HeadObject", error=missing)
    with pytest.raises(FileNotFoundError):
        store.head("user/file/original")
    client.head_object.side_effect = RuntimeError("secret-value-and-signed-url")
    with pytest.raises(StorageUnavailable) as caught:
        store.head("user/file/original")
    assert str(caught.value) == "object_storage_unavailable"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("chunks,declared", [([b"12345"], 5), ([b"12", b"345"], None), ([b"12"], 3)])
def test_bounded_read_closes_stream_on_failure(chunks, declared):
    client = Mock()
    store = OSSStorage(configured(upload_max_bytes=4, pdf_max_bytes=4), client=client)
    body = Body(chunks)
    client.get_object.return_value = SimpleNamespace(content_length=declared, body=body)
    with pytest.raises(StorageUnavailable):
        store.read("user/file/original")
    assert body.closed


def test_default_endpoint_is_https_without_cname(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr("app.modules.files.oss_storage.oss.Client", constructor)
    OSSStorage(configured())
    sdk = constructor.call_args.args[0]
    assert sdk.endpoint == "https://oss-cn-hangzhou.aliyuncs.com"
    assert sdk.signature_version == "v4"
    assert sdk.insecure_skip_verify is False and sdk.enabled_redirect is False
    assert not sdk.use_cname


@pytest.mark.parametrize("endpoint", ["http://oss-cn-hangzhou.aliyuncs.com", "https://localhost",
                                       "https://oss-cn-beijing.aliyuncs.com", "https://assets.example.com",
                                       "https://oss-cn-hangzhou.aliyuncs.com/path"])
def test_config_rejects_untrusted_or_wrong_region_endpoint(endpoint):
    with pytest.raises(ValidationError):
        configured(oss_endpoint=endpoint)


def test_internal_endpoint_and_secret_masking():
    cfg = configured(oss_endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com")
    assert "dummy-test-secret" not in repr(cfg)
    assert "dummy-access-id" not in cfg.model_dump_json()
    with pytest.raises(ValidationError) as caught:
        configured(oss_endpoint="invalid-secret-value")
    assert "dummy-test-secret" not in str(caught.value)
    assert "invalid-secret-value" not in str(caught.value)


@pytest.mark.parametrize("key", ["user/../file", "https://example.com/file", "user\\file", "/user/file"])
def test_invalid_keys_do_not_call_sdk(key):
    client = Mock()
    with pytest.raises(ValueError):
        OSSStorage(configured(), client=client).read(key)
    client.get_object.assert_not_called()
