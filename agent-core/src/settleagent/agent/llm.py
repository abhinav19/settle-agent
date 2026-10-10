"""LLM boundary. The agent depends on the ``LlmClient`` protocol; tests inject fakes.

``AnthropicLlm`` is the only code that talks to the Claude API. It requests schema-valid
JSON via structured outputs (``output_config.format``) and never uses forced ``tool_choice``.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import OutputConfigParam


class LlmError(Exception):
    """The model call failed or returned something unusable (never crashes a run)."""


class LlmRefusal(LlmError):
    """The model declined the request (``stop_reason == 'refusal'``)."""


@dataclass(frozen=True)
class LlmReply:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LlmClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> LlmReply: ...


class AnthropicLlm:
    """Claude via the official async SDK (the SDK itself retries 429/5xx and connection errors)."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        *,
        effort: str | None = "medium",
        max_tokens: int = 4096,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self._effort:
            output_config["effort"] = self._effort
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=cast(OutputConfigParam, output_config),
            )
        except anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LlmRefusal("model refused the request")
        if response.stop_reason == "max_tokens":
            raise LlmError("response truncated at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LlmError("no text block in response")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LlmError(f"response was not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LlmError("response JSON was not an object")
        return LlmReply(
            data=data,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
