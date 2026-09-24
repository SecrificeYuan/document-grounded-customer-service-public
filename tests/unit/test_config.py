from datetime import date

import pytest

from docqa.config import AppConfig
from docqa.errors import ConfigurationError


def test_key_required_only_for_online_commands(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ConfigurationError):
        AppConfig.from_env(require_key=True, as_of=date(2026, 9, 20))

    config = AppConfig.from_env(require_key=False, as_of=date(2026, 9, 20))
    assert config.reference_date == date(2026, 9, 20)
    assert config.api_key is None


def test_key_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-placeholder")

    config = AppConfig.from_env(require_key=True, as_of=None)

    assert "unit-test-placeholder" not in repr(config)


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_key_is_rejected_for_online_commands(monkeypatch, value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", value)

    with pytest.raises(ConfigurationError):
        AppConfig.from_env(require_key=True, as_of=date(2026, 9, 20))


def test_non_https_base_url_is_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://api.example.test")

    with pytest.raises(ConfigurationError):
        AppConfig.from_env(require_key=False, as_of=date(2026, 9, 20))


def test_default_service_settings(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    config = AppConfig.from_env(require_key=False, as_of=date(2026, 9, 20))

    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-flash"
    assert config.reasoning_effort == "high"
    assert config.connect_timeout_seconds == 10
    assert config.read_timeout_seconds == 120
    assert config.question_deadline_seconds == 600
    assert config.transport_attempts == 3
    assert config.semantic_request_limit == 2
    assert config.contract_max_output_tokens == 64_000
    assert config.analysis_max_output_tokens == 24_000
    assert config.repair_max_output_tokens == 24_000
