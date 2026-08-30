"""engine/llm/nim_client.py, tested against a mocked openai.OpenAI client - no
live NIM key required for the translation logic itself."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

openai = pytest.importorskip("openai", reason="optional 'llm' extra not installed - pip install -e '.[llm]'")

from engine.llm.base import Message, RateLimitedError, ToolCall, ToolSpec
from engine.llm.nim_client import NimClient


def _tool_call(id_, name, arguments: dict):
    tc = MagicMock()
    tc.id = id_
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    return tc


def _response(content=None, tool_calls=None, finish_reason="stop", prompt_tokens=10, completion_tokens=5):
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    choice.finish_reason = finish_reason
    response.choices = [choice]
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


def _client_with_mocked_create(mock_response):
    client = NimClient(api_key="test-key")
    client._client.chat.completions.create = MagicMock(return_value=mock_response)
    return client


def test_translates_a_plain_text_response():
    client = _client_with_mocked_create(_response(content="hello"))
    turn = client.complete([Message(role="user", content="hi")], tools=[])
    assert turn.text == "hello"
    assert turn.tool_calls == ()
    assert turn.stop_reason == "end_turn"
    assert turn.input_tokens == 10 and turn.output_tokens == 5


def test_translates_a_tool_call_response():
    client = _client_with_mocked_create(_response(
        tool_calls=[_tool_call("call_1", "get_record", {"source": "payments", "record_id": "pay_1"})],
    ))
    turn = client.complete([Message(role="user", content="hi")], tools=[ToolSpec("get_record", "desc", {})])
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "get_record"
    assert turn.tool_calls[0].arguments == {"source": "payments", "record_id": "pay_1"}


def test_tool_result_message_becomes_a_tool_role_message():
    client = _client_with_mocked_create(_response(content="ok"))
    client.complete([Message(role="tool", content="42", tool_call_id="call_1")], tools=[])
    _, kwargs = client._client.chat.completions.create.call_args
    msg = kwargs["messages"][0]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_1"
    assert msg["content"] == "42"


def test_assistant_tool_calls_round_trip_back_into_messages():
    client = _client_with_mocked_create(_response(content="ok"))
    prior = Message(role="assistant", content=None, tool_calls=(
        ToolCall(id="call_1", name="get_record", arguments={"x": 1}),
    ))
    client.complete([prior], tools=[])
    _, kwargs = client._client.chat.completions.create.call_args
    msg = kwargs["messages"][0]
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"x": 1}


def test_rate_limit_error_is_translated_to_the_generic_type():
    client = NimClient(api_key="test-key")
    client._client.chat.completions.create = MagicMock(
        side_effect=openai.RateLimitError("rate limited", response=MagicMock(status_code=429, request=MagicMock()), body=None)
    )
    with pytest.raises(RateLimitedError):
        client.complete([Message(role="user", content="hi")], tools=[])
