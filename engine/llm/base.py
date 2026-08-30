"""The shapes every LLM backend adapter speaks - deliberately minimal, just
enough to run a tool-calling loop. Nothing here is Anthropic- or OpenAI-shaped;
each client translates to/from its own SDK's native format at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict  # JSON schema for the tool's parameters


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Message:
    """One turn in the conversation.

    role is one of "system" | "user" | "assistant" | "tool".
    - "assistant" messages may carry `tool_calls` (what the model asked to call).
    - "tool" messages are the result of exactly one prior tool call, identified
      by `tool_call_id`.
    """

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class AssistantTurn:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | "error"
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn: ...


class RateLimitedError(Exception):
    """Raised by a client when the backend signals a rate limit (HTTP 429 or
    equivalent) - the agent loop backs off and retries on this specific error,
    not on any other failure."""


class TransientBackendError(Exception):
    """A 5xx / timeout / connection failure from the provider - the request was
    never meaningfully processed, so retrying it is safe and correct. Kept
    separate from RateLimitedError because the two mean different things even
    though both are retryable: a 429 says "slow down", a 504 says "try again".
    A real 550B-parameter endpoint returns these under load, so an agent batch
    that dies on the first one is not production-shaped."""
