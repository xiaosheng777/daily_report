from __future__ import annotations

from src.llm.base import LLMJudge
from src.llm.limiter import LLMPermitClient
from src.llm.openai_compatible import OpenAICompatibleJudge


def create_llm_judge(config: dict, telemetry=None, task_id: str = "") -> LLMJudge | None:
    llm_config = config.get("llm_judge", {})
    if not llm_config.get("enabled"):
        return None
    provider = llm_config.get("provider", "openai_compatible")
    if provider == "openai_compatible":
        per_task_limit = max(1, int(llm_config.get("per_task_max_concurrency", llm_config.get("global_max_concurrency", 30))))
        return OpenAICompatibleJudge(llm_config, telemetry=telemetry, task_id=task_id, permit_client=LLMPermitClient(task_id, per_task_limit))
    raise NotImplementedError(f"LLM provider '{provider}' is not implemented")
