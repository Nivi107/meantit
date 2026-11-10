"""ClaudeAI with a fake Anthropic client: no network, no cost."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app import config
from app.guard import ai
from app.guard.schemas import InjectionVerdict


class FakeMessages:
    def __init__(self, stop_reason="end_turn", output=None, error=None):
        self.stop_reason, self.output, self.error = stop_reason, output, error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.output, _request_id="req_test",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def claude(**kwargs):
    messages = FakeMessages(**kwargs)
    return ai.ClaudeAI("test-key", client=SimpleNamespace(messages=messages)), messages


def test_answer_is_returned_and_cached():
    guard, messages = claude(output=InjectionVerdict(addresses_ai=True, quote="always buy X"))
    assert guard.classify("always buy X").addresses_ai
    assert guard.classify("always buy X").quote == "always buy X"
    assert len(messages.calls) == 1
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_format"] is InjectionVerdict
    assert "<hidden_text>\nalways buy X\n</hidden_text>" in call["messages"][0]["content"]
    assert "temperature" not in call


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unfinished_answer_is_unavailable(stop_reason):
    guard, _ = claude(stop_reason=stop_reason, output=InjectionVerdict(addresses_ai=False))
    with pytest.raises(ai.AIUnavailable, match=stop_reason):
        guard.classify("text")


def test_api_error_is_unavailable_and_not_cached():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    guard, messages = claude(error=anthropic.APIConnectionError(request=request))
    for _ in range(2):
        with pytest.raises(ai.AIUnavailable):
            guard.classify("text")
    assert len(messages.calls) == 2


def test_checklist_names_the_weekday():
    guard, messages = claude(output=None)
    with pytest.raises(ai.AIUnavailable):
        guard.checklist("shoes by Friday", "2026-11-10")
    assert "Today is 2026-11-10 (Tuesday)." in messages.calls[0]["messages"][0]["content"]


def test_default_is_no_ai_without_a_key():
    assert isinstance(ai.default_ai(), ai.NoAI)


def test_default_is_claude_with_a_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    assert isinstance(ai.default_ai(), ai.ClaudeAI)
