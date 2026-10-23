"""Server-only OSS access. Browsers receive bytes, never credentials or OSS URLs."""

import re

import alibabacloud_oss_v2 as oss
from alibabacloud_oss_v2.exceptions import OperationError, ServiceError

from app.modules.files.storage import ObjectInfo


class StorageUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("object_storage_unavailable")


def _missing_object(error):
    for _ in range(8):
        if isinstance(error, ServiceError):
            return error.status_code == 404 and error.code == "NoSuchKey"
        if not isinstance(error, OperationError):
            break
        error = error.unwrap()
    return False


class OSSStorage:
    def __init__(self, cfg, *, client=None):
        self.bucket, self.prefix = cfg.oss_bucket, cfg.oss_prefix
        self.max_bytes = max(cfg.upload_max_bytes, cfg.pdf_max_bytes)
        if client is not None:
            self.client = client
            return
        sdk = oss.config.load_default()
        sdk.region = cfg.oss_region
        sdk.endpoint = cfg.oss_endpoint or f"https://oss-{cfg.oss_region}.aliyuncs.com"
        sdk.signature_version = "v4"
        sdk.credentials_provider = oss.credentials.StaticCredentialsProvider(
            cfg.oss_access_key_id.get_secret_value(), cfg.oss_access_key_secret.get_secret_value(),
            cfg.oss_security_token.get_secret_value() or None)
        sdk.connect_timeout, sdk.readwrite_timeout = 5, 20
        sdk.retry_max_attempts = 2
        sdk.enabled_redirect = False
        sdk.insecure_skip_verify = False
        self.client = oss.Client(sdk)

    def _key(self, key):
        if (not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+", key)
                or any(p in {".", ".."} for p in key.split("/"))):
            raise ValueError("invalid_object_key")
        return self.prefix + key

    def _call(self, method, request):
        try:
            return getattr(self.client, method)(request)
        except Exception as error:
            if _missing_object(error):
                raise FileNotFoundError("object_not_found") from None
            # Provider exceptions can include request URLs or signing headers.
            raise StorageUnavailable() from None

    def put(self, key, data):
        if len(data) > self.max_bytes:
            raise ValueError("object_size_exceeded")
        self._call("put_object", oss.PutObjectRequest(
            bucket=self.bucket, key=self._key(key), body=data,
            content_length=len(data), content_type="application/octet-stream"))

    def head(self, key):
        result = self._call("head_object", oss.HeadObjectRequest(bucket=self.bucket, key=self._key(key)))
        if not isinstance(result.content_length, int) or result.content_length < 0:
            raise StorageUnavailable()
        return ObjectInfo(size=result.content_length)

    def read(self, key):
        result = self._call("get_object", oss.GetObjectRequest(bucket=self.bucket, key=self._key(key)))
        try:
            with result.body as body:
                if result.content_length is not None and result.content_length > self.max_bytes:
                    raise StorageUnavailable()
                chunks, size = [], 0
                for chunk in body.iter_bytes(block_size=256 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise StorageUnavailable()
                    chunks.append(chunk)
                data = b"".join(chunks)
                if result.content_length is not None and size != result.content_length:
                    raise StorageUnavailable()
                return data
        except Exception:
            raise StorageUnavailable() from None

    def delete(self, key):
        # No versionId: with versioning this creates a delete marker, preserving history.
        try:
            self._call("delete_object", oss.DeleteObjectRequest(bucket=self.bucket, key=self._key(key)))
        except FileNotFoundError:
            pass
