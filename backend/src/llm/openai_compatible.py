from __future__ import annotations

import json
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from src.llm.base import LLMJudge, LLMJudgeResult
from src.utils.config import env_or_value
from src.utils.dates import now_iso
from src.utils.runtime_log import exception_trace


_RAW_LOG_LOCK = threading.Lock()


class OpenAICompatibleJudge(LLMJudge):
    def __init__(self, config: dict[str, Any], telemetry=None, task_id: str = "") -> None:
        self.config = config
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.chat_path = str(config.get("chat_path") or "/v1/chat/completions")
        self.api_key = self._load_api_key(config)
        self.model = str(config.get("model") or "")
        self.temperature = float(config.get("temperature", 0))
        self.max_retries = min(1, max(0, int(config.get("max_retries", 1))))
        self.max_tokens = int(config.get("max_tokens", 1024))
        raw_log_path = str(config.get("raw_log_path") or "").strip()
        self.raw_log_path = Path(raw_log_path) if raw_log_path else None
        self.telemetry = telemetry
        self.task_id = task_id

    def judge_duplicate(self, current: dict[str, Any], matched: dict[str, Any], signals: dict[str, Any]) -> LLMJudgeResult:
        if not self.base_url:
            return LLMJudgeResult(True, False, error="llm base_url is empty")
        if not self.api_key:
            return LLMJudgeResult(True, False, error="llm api key is empty")
        payload = self._build_payload(current, matched, signals)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            content = ""
            call_id = uuid.uuid4().hex
            started_at = now_iso()
            started = time.monotonic()
            self._telemetry("start", {"call_id": call_id, "task_id": self.task_id, "started_at": started_at})
            try:
                data = self._post(payload)
                content = self._extract_content(data)
                parsed = self._parse_json(content)
                duration_ms = int((time.monotonic() - started) * 1000)
                self._telemetry("finish", {"call_id": call_id, "finished_at": now_iso(), "duration_ms": duration_ms, "ok": True, "error_type": ""})
                self._log(current, matched, attempt + 1, content, True, "", duration_ms)
                return self._to_result(parsed)
            except Exception as exc:
                last_error = str(exc)
                duration_ms = int((time.monotonic() - started) * 1000)
                error_type = self._error_type(exc)
                self._telemetry("finish", {"call_id": call_id, "finished_at": now_iso(), "duration_ms": duration_ms, "ok": False, "error_type": error_type, "traceback": exception_trace(exc)})
                self._log(current, matched, attempt + 1, content, False, last_error, duration_ms)
                if attempt < self.max_retries and self._retryable(exc):
                    time.sleep(min(2 ** attempt, 4))
                else:
                    break
        return LLMJudgeResult(True, False, error=last_error or "llm call failed")

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code == 429 or 500 <= exc.code <= 599
        return isinstance(exc, (urllib.error.URLError, ConnectionError))

    @staticmethod
    def _error_type(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"http_{exc.code}"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(exc, urllib.error.URLError):
            return "connection"
        return type(exc).__name__.lower()[:80]

    def _telemetry(self, event: str, payload: dict[str, Any]) -> None:
        if not self.telemetry:
            return
        try:
            self.telemetry(event, payload)
        except Exception:
            pass

    def health_check(self) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "error": "base_url empty"}
        if not self.api_key:
            return {"ok": False, "error": "api key empty"}
        payload = {"model": self.model, "messages": [{"role": "user", "content": "只返回 JSON：{\"ok\":true}"}], "temperature": 0, "max_tokens": 64}
        try:
            data = self._post(payload)
            content = self._extract_content(data)
            return {"ok": True, "content": content[:500]}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _load_api_key(config: dict[str, Any]) -> str:
        """Prefer a mounted secret file while retaining old env/value deployments."""
        key_file = str(config.get("api_key_file") or "").strip()
        if key_file:
            try:
                value = OpenAICompatibleJudge._normalize_api_key(Path(key_file).read_text(encoding="utf-8"))
                if value:
                    return value
            except OSError:
                pass
        return OpenAICompatibleJudge._normalize_api_key(
            env_or_value(str(config.get("api_key") or ""), str(config.get("api_key_env") or ""))
        )

    @staticmethod
    def _normalize_api_key(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        # Support env-style files: DAILY_REPORT_LLM_API_KEY='sk-...'
        if "\n" in text:
            text = next((line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")), "")
        if "=" in text and not text.startswith("sk-"):
            text = text.split("=", 1)[1].strip()
        if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
            text = text[1:-1].strip()
        return text.strip()

    def _build_payload(self, current: dict[str, Any], matched: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any]:
        system_prompt = (
            "你是企业日报重复提交判断系统。判断当前日报相比候选日报是否存在实质新进展。"
            "请特别区分：本人重复提交、跨人复制、正常多人协作。"
            "如果是多人协作且职责、模块、产出不同，不要直接判 high。"
            "只输出合法 JSON，字段：duplicate_risk, judgement_status, confidence, decision_summary, need_human_review。"
            "duplicate_risk 只能是 high, medium, low, normal, unknown。"
        )
        user_prompt = {"current_report": current, "matched_report": matched, "signals": signals, "instruction": "判断是否重复提交，给出简短中文理由。"}
        return {"model": self.model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)}], "temperature": self.temperature, "max_tokens": self.max_tokens, "response_format": {"type": "json_object"}}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + self.chat_path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # The duplicate-check worker owns the only time limit: a hard one-hour task timeout.
        with opener.open(request) as response:
            return json.load(response)

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        if "choices" in data:
            return str(data["choices"][0]["message"]["content"]).strip()
        if "message" in data and isinstance(data["message"], dict):
            return str(data["message"].get("content", "")).strip()
        if "content" in data:
            return str(data["content"]).strip()
        raise ValueError("cannot find model content")

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        cleaned = content.strip().replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end + 1])
            raise

    @staticmethod
    def _to_result(parsed: dict[str, Any]) -> LLMJudgeResult:
        risk = str(parsed.get("duplicate_risk") or "unknown").lower().strip()
        if risk not in {"high", "medium", "low", "normal", "unknown"}:
            risk = "unknown"
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0))))
        except Exception:
            confidence = 0.0
        return LLMJudgeResult(True, True, risk, str(parsed.get("judgement_status") or "valid"), confidence, str(parsed.get("decision_summary") or parsed.get("reason") or ""), bool(parsed.get("need_human_review", risk in {"high", "unknown"})), parsed)

    def _log(self, current: dict[str, Any], matched: dict[str, Any], attempt: int, content: str, ok: bool, error: str, duration_ms: int = 0) -> None:
        if not self.raw_log_path:
            return
        record = {"logged_at": now_iso(), "model": self.model, "attempt": attempt, "ok": ok, "error": error, "duration_ms": duration_ms, "current_report_id": current.get("report_id"), "matched_report_id": matched.get("report_id"), "raw_content": content}
        with _RAW_LOG_LOCK:
            self.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.raw_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
