"""engine/llm/anthropic_client.py, tested against a mocked anthropic.Anthropic
client - no live key required. This is what makes the Anthropic backend
spec-complete and trustworthy even though it was never run for real (see
ARCHITECTURE.md for why NimClient was used for the actual Phase 5 benchmark).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import pytest

from engine.llm.anthropic_client import AnthropicClient
from engine.llm.base import Message, RateLimitedError, ToolSpec


def _text_block(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(id_, name, input_):
    block = MagicMock()
    block.type = "tool_use"
    block.id = id_
    block.name = name
    block.input = input_
    return block


def _response(content_blocks, stop_reason="end_turn", input_tokens=10, output_tokens=5):
    response = MagicMock()
    response.content = content_blocks
    response.stop_reason = stop_reason
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


def _client_with_mocked_create(mock_response):
    client = AnthropicClient(api_key="test-key")
    client._client.messages.create = MagicMock(return_value=mock_response)
    return client


def test_translates_a_plain_text_response():
    client = _client_with_mocked_create(_response([_text_block("hello")]))
    turn = client.complete([Message(role="user", content="hi")], tools=[])
    assert turn.text == "hello"
    assert turn.tool_calls == ()
    assert turn.stop_reason == "end_turn"
    assert turn.input_tokens == 10 and turn.output_tokens == 5


def test_translates_a_tool_use_response():
    client = _client_with_mocked_create(_response(
        [_tool_use_block("tu_1", "get_record", {"source": "payments", "record_id": "pay_1"})],
        stop_reason="tool_use",
    ))
    turn = client.complete([Message(role="user", content="hi")], tools=[ToolSpec("get_record", "desc", {})])
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "tu_1"
    assert turn.tool_calls[0].name == "get_record"
    assert turn.tool_calls[0].arguments == {"source": "payments", "record_id": "pay_1"}


def test_system_messages_go_to_the_system_param_not_the_message_list():
    client = _client_with_mocked_create(_response([_text_block("ok")]))
    client.complete([Message(role="system", content="SYSTEM PROMPT"), Message(role="user", content="hi")], tools=[])
    _, kwargs = client._client.messages.create.call_args
    assert kwargs["system"] == "SYSTEM PROMPT"
    assert all(m["role"] != "system" for m in kwargs["messages"])


def test_tool_result_message_becomes_a_user_tool_result_block():
    client = _client_with_mocked_create(_response([_text_block("ok")]))
    client.complete([Message(role="tool", content="42", tool_call_id="tu_1")], tools=[])
    _, kwargs = client._client.messages.create.call_args
    msg = kwargs["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"][0]["type"] == "tool_result"
    assert msg["content"][0]["tool_use_id"] == "tu_1"
    assert msg["content"][0]["content"] == "42"


def test_rate_limit_error_is_translated_to_the_generic_type():
    client = AnthropicClient(api_key="test-key")
    client._client.messages.create = MagicMock(
        side_effect=anthropic.RateLimitError("rate limited", response=MagicMock(), body=None)
    )
    with pytest.raises(RateLimitedError):
        client.complete([Message(role="user", content="hi")], tools=[])


def test_no_tools_sends_not_given_sentinel():
    client = _client_with_mocked_create(_response([_text_block("ok")]))
    client.complete([Message(role="user", content="hi")], tools=[])
    _, kwargs = client._client.messages.create.call_args
    assert kwargs["tools"] is anthropic.NOT_GIVEN
