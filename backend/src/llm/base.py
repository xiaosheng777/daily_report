from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class LLMJudgeResult:
    called: bool
    ok: bool
    duplicate_risk: str = "unknown"
    judgement_status: str = "unknown"
    confidence: float = 0.0
    decision_summary: str = ""
    need_human_review: bool = True
    raw: dict[str, Any] | None = None
    error: str = ""


class LLMJudge(ABC):
    @abstractmethod
    def judge_duplicate(self, current: dict[str, Any], matched: dict[str, Any], signals: dict[str, Any]) -> LLMJudgeResult: ...

    @abstractmethod
    def health_check(self) -> dict[str, Any]: ...
