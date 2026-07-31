from __future__ import annotations

from src.llm.base import LLMJudge
from src.llm.openai_compatible import OpenAICompatibleJudge


def create_llm_judge(config: dict) -> LLMJudge | None:
    llm_config = config.get("llm_judge", {})
    if not llm_config.get("enabled"):
        return None
    provider = llm_config.get("provider", "openai_compatible")
    if provider == "openai_compatible":
        return OpenAICompatibleJudge(llm_config)
    raise NotImplementedError(f"LLM provider '{provider}' is not implemented")
