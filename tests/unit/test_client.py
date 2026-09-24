from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docqa.errors import ConfigurationError, InvalidModelReply, TransportExhausted
from docqa.llm_client import DeepSeekClient, ModelRequest
from docqa.safe_logging import error_event, request_event
from tests.fake_llm import FakeLLMClient
from tests.fixtures.builders import make_config, make_request, raw_reply


def test_api_envelope_and_sdk_retry_disabled() -> None:
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(id="r1", status="completed", model="deepseek-flash", output_text='{"ok":true}', usage=None))))
    client = DeepSeekClient(make_config(), sdk=sdk)
    reply = client.request(make_request())
    args = sdk.responses.create.call_args.kwargs
    assert args["reasoning"] == {"effort": "high"}
    assert args["text"]["format"]["type"] == "json_schema"
    assert args["text"]["format"]["name"] == "unit_test_object"
    assert args["text"]["format"]["schema"]["required"] == ["ok"]
    assert args["stream"] is False
    assert reply.text == '{"ok":true}'
    assert reply.usage.input_tokens is None


def test_constructed_sdk_disables_its_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=Mock()))

    monkeypatch.setattr("docqa.llm_client.openai.OpenAI", fake_openai)
    DeepSeekClient(make_config())
    assert captured["max_retries"] == 0
    assert type(captured["timeout"]).__name__ == "Timeout"


@pytest.mark.parametrize("name", ["", "has space", "x" * 65, "名字"])
def test_schema_name_is_restricted(name: str) -> None:
    with pytest.raises(ValueError):
        make_request(schema_name=name)


@pytest.mark.parametrize("status,text", [("completed", ""), ("incomplete", "{}"), ("failed", "{}"), ("mystery", "{}")])
def test_noncompleted_or_empty_reply_is_semantically_invalid(status: str, text: str) -> None:
    with pytest.raises(InvalidModelReply):
        raw_reply(text, status).require_completed_text()


def test_fake_client_is_bounded() -> None:
    client = FakeLLMClient([raw_reply("{}")])
    assert client.request(make_request()).text == "{}"
    with pytest.raises(AssertionError, match="unexpected"):
        client.request(make_request())


class FakeTransportError(Exception):
    def __init__(self, status_code=None, retry_after=None) -> None:
        super().__init__("secret server body")
        self.status_code = status_code
        self.response = SimpleNamespace(headers={"Retry-After": retry_after} if retry_after is not None else {})


def test_three_rate_limits_exhaust_transport_without_sdk_retry() -> None:
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=[FakeTransportError(429), FakeTransportError(429), FakeTransportError(429)])))
    sleeps: list[float] = []
    client = DeepSeekClient(make_config(), sdk=sdk, sleep=sleeps.append)
    with pytest.raises(TransportExhausted):
        client.request(make_request())
    assert sdk.responses.create.call_count == 3
    assert sleeps == [1.0, 2.0]


def test_unauthorized_stops_without_retry() -> None:
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=FakeTransportError(401))))
    client = DeepSeekClient(make_config(), sdk=sdk, sleep=lambda _: None)
    with pytest.raises(ConfigurationError):
        client.request(make_request())
    assert sdk.responses.create.call_count == 1


def test_timeout_then_success_and_retry_after_deadline() -> None:
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=[TimeoutError(), SimpleNamespace(id="r2", status="completed", model="deepseek-flash", output_text="{}", usage=None)])))
    sleeps: list[float] = []
    client = DeepSeekClient(make_config(), sdk=sdk, sleep=sleeps.append)
    assert client.request(make_request()).response_id == "r2"
    assert sleeps == [1.0]

    late_sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=FakeTransportError(429, "700"))))
    with pytest.raises(TransportExhausted, match="deadline"):
        DeepSeekClient(make_config(), sdk=late_sdk, sleep=lambda _: pytest.fail("must not sleep")).request(make_request())


def test_retry_after_http_date_is_honored() -> None:
    retry_at = "Wed, 02 Jan 2030 00:00:10 GMT"
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=[FakeTransportError(429, retry_at), SimpleNamespace(id="r3", status="completed", model="deepseek-flash", output_text="{}", usage=None)])))
    sleeps: list[float] = []
    client = DeepSeekClient(make_config(), sdk=sdk, sleep=sleeps.append, now=lambda: datetime(2030, 1, 2, tzinfo=timezone.utc))
    assert client.request(make_request()).response_id == "r3"
    assert sleeps == [10.0]


def test_safe_diagnostics_are_whitelisted() -> None:
    reply = raw_reply('{"prompt":"do not log"}')
    encoded = json.dumps(request_event(reply, attempt=1), ensure_ascii=False)
    encoded += json.dumps(error_event(FakeTransportError(429), attempt=2), ensure_ascii=False)
    assert "unit-test-placeholder" not in encoded
    assert "Authorization" not in encoded
    assert "prompt" not in encoded
    assert '"reasoning":' not in encoded
    assert "do not log" not in encoded
    assert "secret server body" not in encoded
    assert "error_code" in encoded
