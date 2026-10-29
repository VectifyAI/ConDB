import copy
import json
from types import SimpleNamespace

import litellm
import pytest

from contextdb.llm import LLMClient

RANK_TOOL = [{"name": "rank", "description": "Rank ids", "input_schema": {"type": "object", "properties": {}}}]
FORCED_RANK = {"type": "function", "function": {"name": "rank"}}


def _response(text=None, ranked_ids=None, prompt_tokens=10, completion_tokens=5, cached_tokens=None):
    tool_calls = None
    if ranked_ids is not None:
        arguments = json.dumps({"ranked_ids": ranked_ids})
        tool_calls = [SimpleNamespace(id="call_1", function=SimpleNamespace(name="rank", arguments=arguments))]
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=None if cached_tokens is None else {"cached_tokens": cached_tokens},
    )
    finish_reason = "tool_calls" if tool_calls else "stop"
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage)


class FakeCompletion:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr("contextdb.llm.time.sleep", recorded.append)
    return recorded


def _client(outcomes, provider="openai", model="gpt-4o", max_retries=4):
    client = LLMClient(provider=provider, model=model, max_retries=max_retries)
    completion = FakeCompletion(outcomes)
    client._litellm = SimpleNamespace(completion=completion)
    return client, completion


def _overloaded():
    return litellm.InternalServerError(message="Overloaded", llm_provider="anthropic", model="claude")


def test_transient_errors_are_retried(sleeps):
    client, completion = _client([_overloaded(), _overloaded(), _response(ranked_ids=["a"])])

    result = client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 3
    assert len(sleeps) == 2
    assert result["content"][0]["input"] == {"ranked_ids": ["a"]}


def test_sdk_retries_are_disabled(sleeps):
    client, completion = _client([_overloaded(), _response(ranked_ids=["a"])])

    client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert [call["max_retries"] for call in completion.calls] == [0, 0]


@pytest.mark.parametrize(
    "error",
    [
        litellm.RateLimitError(message="429", llm_provider="anthropic", model="claude"),
        litellm.BadGatewayError(message="502", llm_provider="anthropic", model="claude"),
        litellm.ServiceUnavailableError(message="503", llm_provider="anthropic", model="claude"),
        litellm.Timeout(message="timeout", model="claude", llm_provider="anthropic"),
    ],
)
def test_each_transient_error_kind_is_retried(sleeps, error):
    client, completion = _client([error, _response(ranked_ids=["a"])])

    client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 2


def test_retries_stop_after_max_retries(sleeps):
    client, completion = _client([_overloaded() for _ in range(3)], max_retries=2)

    with pytest.raises(litellm.InternalServerError):
        client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "error",
    [
        litellm.BadRequestError(message="bad", model="claude", llm_provider="anthropic"),
        # litellm wraps errors it cannot map, such as an Anthropic 403, in APIConnectionError.
        litellm.APIConnectionError(message="permission_error", llm_provider="anthropic", model="claude"),
    ],
)
def test_permanent_errors_are_not_retried(sleeps, error):
    client, completion = _client([error])

    with pytest.raises(type(error)):
        client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 1
    assert sleeps == []


def test_tool_reply_makes_one_call_without_tool_choice(sleeps):
    client, completion = _client([_response(ranked_ids=["a"])])

    client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 1
    assert "tool_choice" not in completion.calls[0]


def test_text_reply_is_asked_again_with_tool_forced(sleeps):
    client, completion = _client([
        _response(text="The best match is a.", prompt_tokens=100, completion_tokens=8, cached_tokens=60),
        _response(ranked_ids=["a"], prompt_tokens=100, completion_tokens=12),
    ])

    result = client.chat([{"role": "user", "content": "q"}], tools=RANK_TOOL)

    assert len(completion.calls) == 2
    assert "tool_choice" not in completion.calls[0]
    assert completion.calls[1]["tool_choice"] == FORCED_RANK
    assert completion.calls[1]["messages"] == completion.calls[0]["messages"]
    assert [block["type"] for block in result["content"]] == ["tool_use"]
    assert result["usage"]["input_tokens"] == 200
    assert result["usage"]["output_tokens"] == 20
    assert result["usage"]["cached_tokens"] == 60


def test_cached_text_reply_is_asked_again_with_tool_forced(sleeps):
    client, completion = _client(
        [_response(text="a"), _response(ranked_ids=["a"])],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = client.chat_with_cache(
        [{"role": "user", "content": "q"}],
        tools=RANK_TOOL,
        cache_content=["block"],
    )

    assert len(completion.calls) == 2
    assert completion.calls[1]["tool_choice"] == FORCED_RANK
    assert result["content"][0]["type"] == "tool_use"


def test_text_reply_without_tools_is_returned(sleeps):
    client, completion = _client([_response(text="answer")])

    result = client.chat([{"role": "user", "content": "q"}])

    assert len(completion.calls) == 1
    assert result["content"] == [{"type": "text", "text": "answer"}]
