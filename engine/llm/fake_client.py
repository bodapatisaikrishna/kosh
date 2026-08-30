"""A scripted LLMClient for tests - no network, fully deterministic. Constructed
with a fixed sequence of AssistantTurns to hand back on successive complete()
calls, so a test can script exactly what "the model" does at each step and
assert how the agent loop reacts (accepts, rejects, forces a retry, etc.).
"""

from __future__ import annotations

import threading

from .base import AssistantTurn, LLMClient, Message, RateLimitedError, ToolSpec


class FakeClient(LLMClient):
    """Scripted client. Two modes:

    - `turns`: one flat queue, served in order. Fine for a single-record test.
    - `turns_by_record`: {record_id: [turns]}, matched against the record named in
      the conversation. Required for any test that drives run_l3 over MORE THAN
      ONE record, because run_l3 runs records concurrently - with a single shared
      queue, which record receives which scripted turn is a race, and the test
      passes or fails depending on interleaving.
    """

    def __init__(
        self,
        turns: list[AssistantTurn] | None = None,
        raise_rate_limit_on_call: int | None = None,
        turns_by_record: dict[str, list[AssistantTurn]] | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self._turns_by_record = {k: list(v) for k, v in (turns_by_record or {}).items()}
        self._raise_rate_limit_on_call = raise_rate_limit_on_call
        self._lock = threading.Lock()
        self.call_count = 0
        self.received_messages: list[list[Message]] = []

    def _record_id_in(self, messages: list[Message]) -> str | None:
        text = " ".join(m.content or "" for m in messages if m.role == "user")
        # longest first, so "pay_12" is not shadowed by a "pay_1" prefix
        for record_id in sorted(self._turns_by_record, key=len, reverse=True):
            if record_id in text:
                return record_id
        return None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        # run_l3 dispatches records across threads; without this the counters and
        # the queues themselves race.
        with self._lock:
            self.call_count += 1
            self.received_messages.append(list(messages))
            if self._raise_rate_limit_on_call == self.call_count:
                raise RateLimitedError("scripted 429")

            if self._turns_by_record:
                record_id = self._record_id_in(messages)
                if record_id is None:
                    raise AssertionError(f"FakeClient: no scripted record matched this conversation (known: {sorted(self._turns_by_record)})")
                queue = self._turns_by_record[record_id]
                if not queue:
                    raise AssertionError(f"FakeClient ran out of scripted turns for {record_id}")
                return queue.pop(0)

            if not self._turns:
                raise AssertionError("FakeClient ran out of scripted turns - the agent loop called complete() more times than the test expected")
            return self._turns.pop(0)
