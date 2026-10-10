"""AnthropicLlm against a fake SDK client: request shape and every failure mode."""

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from settleagent.agent.llm import AnthropicLlm, LlmError, LlmRefusal

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def response(text: str | None = '{"a": 1}', stop: str = "end_turn") -> Any:
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        stop_reason=stop,
        content=content,
        model="claude-test",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def llm(client: FakeClient, **kw: Any) -> AnthropicLlm:
    return AnthropicLlm(client, "claude-opus-5-5", **kw)  # type: ignore[arg-type]


async def test_success_returns_parsed_json_and_usage() -> None:
    client = FakeClient(response())
    reply = await llm(client, effort="low", max_tokens=999).complete_json(
        system="sys", user="usr", schema=SCHEMA
    )
    assert reply.data == {"a": 1} and reply.model == "claude-test"
    assert (reply.input_tokens, reply.output_tokens, reply.total_tokens) == (11, 7, 18)


async def test_request_uses_structured_outputs_and_never_forced_tool_choice() -> None:
    client = FakeClient(response())
    await llm(client, effort="low", max_tokens=999).complete_json(
        system="sys", user="usr", schema=SCHEMA
    )
    [req] = client.requests
    assert req["model"] == "claude-opus-5-5" and req["max_tokens"] == 999
    assert req["system"] == "sys"
    assert req["messages"] == [{"role": "user", "content": "usr"}]
    assert req["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "low",
    }
    assert "tool_choice" not in req and "thinking" not in req and "tools" not in req
    assert "temperature" not in req


async def test_effort_can_be_omitted() -> None:
    client = FakeClient(response())
    await llm(client, effort=None).complete_json(system="s", user="u", schema=SCHEMA)
    assert "effort" not in client.requests[0]["output_config"]


async def test_refusal_is_a_distinct_error() -> None:
    with pytest.raises(LlmRefusal):
        await llm(FakeClient(response(stop="refusal"))).complete_json(
            system="s", user="u", schema=SCHEMA
        )


@pytest.mark.parametrize(
    ("resp", "fragment"),
    [
        (response(stop="max_tokens"), "truncated"),
        (response(text=None), "no text block"),
        (response(text="not json"), "not valid JSON"),
        (response(text=json.dumps([1, 2])), "not an object"),
    ],
)
async def test_unusable_responses_raise_llm_error(resp: Any, fragment: str) -> None:
    with pytest.raises(LlmError, match=fragment):
        await llm(FakeClient(resp)).complete_json(system="s", user="u", schema=SCHEMA)


async def test_api_errors_are_wrapped() -> None:
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    with pytest.raises(LlmError, match="APIConnectionError"):
        await llm(FakeClient(err)).complete_json(system="s", user="u", schema=SCHEMA)
