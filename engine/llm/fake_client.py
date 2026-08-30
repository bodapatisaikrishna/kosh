"""A scripted LLMClient for tests - no network, fully deterministic. Constructed
with a fixed sequence of AssistantTurns to hand back on successive complete()
calls, so a test can script exactly what "the model" does at each step and
assert how the agent loop reacts (accepts, rejects, forces a retry, etc.).
"""

from __future__ import annotations

from .base import AssistantTurn, LLMClient, Message, RateLimitedError, ToolSpec


class FakeClient(LLMClient):
    def __init__(self, turns: list[AssistantTurn], raise_rate_limit_on_call: int | None = None) -> None:
        self._turns = list(turns)
        self._raise_rate_limit_on_call = raise_rate_limit_on_call
        self.call_count = 0
        self.received_messages: list[list[Message]] = []

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        self.call_count += 1
        self.received_messages.append(list(messages))
        if self._raise_rate_limit_on_call == self.call_count:
            raise RateLimitedError("scripted 429")
        if not self._turns:
            raise AssertionError("FakeClient ran out of scripted turns - the agent loop called complete() more times than the test expected")
        return self._turns.pop(0)
