"""Real LLMClient backed by the Anthropic SDK - the brief's specified stack.

Built to the same LLMClient interface as NimClient and unit-tested against a
mocked anthropic.Anthropic client (see tests/test_anthropic_client.py) since no
live Anthropic key was available when this was written. Ready to use for real
the moment ANTHROPIC_API_KEY is set - nothing else needs to change.
"""

from __future__ import annotations

import os

from .base import AssistantTurn, LLMClient, Message, RateLimitedError, ToolCall, ToolSpec

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096


def _to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]


def _extract_system_prompt(messages: list[Message]) -> str | None:
    system_messages = [m.content for m in messages if m.role == "system" and m.content]
    return "\n\n".join(system_messages) if system_messages else None


def _to_anthropic_messages(messages: list[Message]) -> list[dict]:
    """Anthropic has no "system" or "tool" role: system prompts are a separate
    top-level param (stripped here, see _extract_system_prompt), and a tool
    result is sent as a "user" message with a tool_result content block."""
    out = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or ""}]})
        elif m.role == "assistant" and m.tool_calls:
            blocks = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments} for tc in m.tool_calls]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


class AnthropicClient(LLMClient):
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        import anthropic  # imported lazily so the package is only required when this client is actually used

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)
        # The brief's env-var cheap-tier note: ANTHROPIC_MODEL can override the
        # default claude-sonnet-5 with e.g. claude-haiku-4-5-20251001.
        self._model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=_extract_system_prompt(messages),
                messages=_to_anthropic_messages(messages),
                tools=_to_anthropic_tools(tools) if tools else anthropic.NOT_GIVEN,
            )
        except anthropic.RateLimitError as exc:
            raise RateLimitedError(str(exc)) from exc

        text = None
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        return AssistantTurn(
            text=text,
            tool_calls=tuple(tool_calls),
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
