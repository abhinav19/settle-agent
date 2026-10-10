"""Versioned prompt templates. Bump the version when wording or schemas change."""

from settleagent.agent.prompts.v1 import (
    ANALYZE_SCHEMA,
    PLAN_SCHEMA,
    PROMPT_VERSION,
    SYNTHESIZE_SCHEMA,
    SYSTEM_PROMPT,
    analyze_prompt,
    plan_prompt,
    repair_prompt,
    synthesize_prompt,
)

__all__ = [
    "ANALYZE_SCHEMA",
    "PLAN_SCHEMA",
    "PROMPT_VERSION",
    "SYNTHESIZE_SCHEMA",
    "SYSTEM_PROMPT",
    "analyze_prompt",
    "plan_prompt",
    "repair_prompt",
    "synthesize_prompt",
]
