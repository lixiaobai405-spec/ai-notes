"""Shared bounded values for operator-only, reference-aware file cleanup."""
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_OBJECTS = 50_000
MAX_OBJECT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MIN_GRACE_DAYS = 7


class FileGCError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="gc_invalid"):
    if not condition:
        raise FileGCError(code) from None


def timestamp(value):
    require(isinstance(value, str), "gc_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "gc_time_invalid")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise FileGCError("gc_time_invalid") from None


@dataclass(frozen=True)
class ObjectRecord:
    key: str
    size: int
    modified_at: str
    identity: dict

    def document(self):
        return {"key": self.key, "size": self.size, "modified_at": self.modified_at,
                "identity": self.identity}
