"""Real LLMClient backed by NVIDIA NIM's OpenAI-compatible endpoint.

NIM doesn't host Claude - it serves open models (Llama, Nemotron, ...) via an
API shaped like OpenAI's. This is what's actually wired up for the reference
Phase 5 benchmark, in place of the brief's specified Anthropic stack, because
that's the key available when this was built. See ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import os

from .base import AssistantTurn, LLMClient, Message, RateLimitedError, ToolCall, ToolSpec, TransientBackendError

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.3-70b-instruct"


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
        elif m.role == "assistant" and m.tool_calls:
            out.append({
                "role": "assistant",
                "content": m.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in m.tool_calls
                ],
            })
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


class NimClient(LLMClient):
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL) -> None:
        import openai  # imported lazily so the package is only required when this client is actually used

        key = api_key or os.environ.get("NIM_API_KEY")
        if not key:
            raise RuntimeError("NIM_API_KEY is not set - export it in your shell before using NimClient")
        self._client = openai.OpenAI(base_url=base_url, api_key=key)
        self._model = model

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=_to_openai_messages(messages),
                tools=_to_openai_tools(tools) if tools else None,
            )
        except openai.RateLimitError as exc:
            raise RateLimitedError(str(exc)) from exc
        except (openai.InternalServerError, openai.APITimeoutError, openai.APIConnectionError) as exc:
            # 5xx / timeout / connection drop: the request never produced a
            # decision, so retrying is safe. Observed for real against this
            # endpoint (a 504 mid-batch), which is what motivated handling it.
            raise TransientBackendError(str(exc)) from exc

        choice = response.choices[0]
        raw_tool_calls = choice.message.tool_calls or []
        tool_calls = tuple(
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
            for tc in raw_tool_calls
        )
        stop_reason = "tool_use" if tool_calls else ("max_tokens" if choice.finish_reason == "length" else "end_turn")
        usage = response.usage
        return AssistantTurn(
            text=choice.message.content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )
