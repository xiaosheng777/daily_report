from __future__ import annotations

import json
import re
import sys
import traceback
from typing import Any

from src.utils.dates import now_iso


_SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|session|credential)", re.I)
_SENSITIVE_TEXT = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie|session|credential)\b\s*([=:])\s*(['\"]?)([^,\s'\"}\]]+)\3")
_SENSITIVE_OBJECT = re.compile(r"(?i)([\"'](?:password|passwd|secret|token|api[_-]?key|authorization|cookie|session|credential)[\"']\s*:\s*)[\"'][^\"']*[\"']")
_BEARER_TEXT = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def redact_runtime_text(value: str) -> str:
    text = str(value or "")
    text = _SENSITIVE_OBJECT.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    return _BEARER_TEXT.sub("Bearer [redacted]", text)


def redact_runtime_value(value: Any, key: str = "") -> Any:
    """Return a JSON-safe diagnostic value without credentials or request content."""
    if _SENSITIVE_KEY.search(str(key)):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): redact_runtime_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_runtime_value(item, key) for item in value]
    if isinstance(value, str):
        return redact_runtime_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_runtime_text(str(value))


def exception_trace(exc: BaseException) -> str:
    return redact_runtime_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def runtime_log(
    db,
    source: str,
    event: str,
    message: str,
    *,
    level: str = "info",
    task_id: str = "",
    attempt_no: int | None = None,
    context: dict[str, Any] | None = None,
    exc: BaseException | None = None,
    traceback_text: str = "",
) -> dict[str, Any] | None:
    """Mirror an application event to stdout and the shared administrator log."""
    payload = {
        "occurred_at": now_iso(), "level": level if level in {"info", "warning", "error"} else "info",
        "source": source, "event": event, "message": redact_runtime_text(message),
        "task_id": str(task_id or ""), "attempt_no": int(attempt_no or 0),
        "context": redact_runtime_value(context or {}), "traceback": exception_trace(exc) if exc else redact_runtime_text(traceback_text),
    }
    print(f"[{payload['occurred_at']}] {payload['level'].upper()} {source}.{event}: {payload['message']}", file=sys.stderr if payload["level"] == "error" else sys.stdout, flush=True)
    append = getattr(db, "append_runtime_log", None)
    if not callable(append):
        return None
    try:
        return append(payload)
    except Exception as write_error:  # Logging must never interrupt the business operation.
        print(f"[{now_iso()}] ERROR runtime_log.write_failed: {type(write_error).__name__}", file=sys.stderr, flush=True)
        return None


def runtime_log_json(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
