from functools import lru_cache
from typing import Literal
import re
from urllib.parse import urlsplit
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: str = "postgresql+psycopg://notes:notes@localhost:5432/notes"
    app_env: str = "development"
    app_origin: str = "http://localhost:5173"
    cookie_secure: bool = False
    session_days: int = 7
    rate_limit_requests: int = 10
    rate_limit_seconds: int = 300
    job_lease_seconds: int = 30
    db_pool_size: int = 5
    db_max_overflow: int = 5
    storage_backend: Literal["local", "oss"] = "local"
    storage_root: str = ".local/files"
    upload_max_bytes: int = 20 * 1024 * 1024
    upload_max_pixels: int = 40_000_000
    upload_batch_limit: int = 50
    storage_quota_bytes: int = 1024 * 1024 * 1024
    upload_expiry_seconds: int = 600
    file_ticket_seconds: int = 300
    upload_read_timeout_seconds: int = 60
    pdf_max_bytes: int = 50 * 1024 * 1024
    pdf_max_pages: int = 200
    pdf_page_timeout_seconds: int = 60
    export_max_bytes: int = 512 * 1024 * 1024
    export_max_rows: int = 50_000
    export_max_metadata_bytes: int = 32 * 1024 * 1024
    export_max_seconds: int = 120
    oss_region: str = "cn-hangzhou"
    oss_bucket: str = ""
    oss_endpoint: str = ""
    oss_prefix: str = "ai-notes/"
    oss_access_key_id: SecretStr = SecretStr("")
    oss_access_key_secret: SecretStr = SecretStr("")
    oss_security_token: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")

    @field_validator("deepseek_api_key")
    @classmethod
    def validate_deepseek_api_key(cls, value):
        raw = value.get_secret_value()
        if raw and (not 16 <= len(raw) <= 512 or any(ord(character) < 33 or ord(character) > 126 for character in raw)):
            raise ValueError("DeepSeek API Key must contain 16 to 512 visible ASCII characters")
        return value

    @model_validator(mode="after")
    def validate_production(self):
        if self.app_env == "production" and (
            not self.cookie_secure or not self.app_origin.startswith("https://")
        ):
            raise ValueError("Production requires HTTPS origin and secure cookies")
        if min(self.session_days, self.rate_limit_requests, self.rate_limit_seconds, self.job_lease_seconds) < 1:
            raise ValueError("Limits must be positive")
        if self.db_pool_size < 1 or self.db_max_overflow < 0:
            raise ValueError("Invalid database pool limits")
        if self.pdf_max_pages > 500:
            raise ValueError("PDF page limit must not exceed the reader's 500-page ordering limit")
        if min(self.upload_max_bytes, self.upload_max_pixels, self.upload_batch_limit,
               self.storage_quota_bytes, self.upload_expiry_seconds, self.file_ticket_seconds,
               self.upload_read_timeout_seconds, self.pdf_max_bytes, self.pdf_max_pages,
               self.pdf_page_timeout_seconds) < 1:
            raise ValueError("File limits must be positive")
        if (min(self.export_max_bytes, self.export_max_rows,
                self.export_max_metadata_bytes, self.export_max_seconds) < 1
                or self.export_max_bytes > 512 * 1024 * 1024
                or self.export_max_rows > 50_000
                or self.export_max_metadata_bytes > 32 * 1024 * 1024):
            raise ValueError("Export limits must be positive and within the v1 format limits")
        if self.app_env == "production" and self.storage_backend == "local":
            raise ValueError("Production requires configured OSS storage")
        if self.storage_backend == "oss":
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.oss_bucket):
                raise ValueError("A valid OSS bucket is required")
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", self.oss_region):
                raise ValueError("A valid OSS region is required")
            if not self.oss_access_key_id.get_secret_value() or not self.oss_access_key_secret.get_secret_value():
                raise ValueError("OSS credentials are required")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*/", self.oss_prefix):
                raise ValueError("OSS prefix must be a dedicated directory")
            if self.oss_endpoint:
                endpoint = urlsplit(self.oss_endpoint)
                allowed = {f"oss-{self.oss_region}.aliyuncs.com",
                           f"oss-{self.oss_region}-internal.aliyuncs.com"}
                if (endpoint.scheme != "https" or endpoint.netloc not in allowed
                        or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
                    raise ValueError("OSS endpoint must be the HTTPS regional or internal endpoint")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
