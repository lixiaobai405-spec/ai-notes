"""Small shared contracts for private, read-only operator monitoring."""
from datetime import datetime, timezone

MAX_OUTPUT_BYTES = 64 * 1024
MAX_HISTORY = 100


class MonitoringError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="monitoring_invalid"):
    if not condition:
        raise MonitoringError(code) from None


def unavailable(code):
    return {"status": "unavailable", "code": code}


def timestamp(value):
    require(isinstance(value, str), "monitoring_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "monitoring_time_invalid")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise MonitoringError("monitoring_time_invalid") from None
