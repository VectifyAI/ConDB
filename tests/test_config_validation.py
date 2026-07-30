import pytest

from contextdb.config import Config


def test_validate_requires_key_for_configured_anthropic_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        Config.validate()


def test_validate_requires_key_for_configured_openai_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", None)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Config.validate()


def test_validate_rejects_unsupported_provider(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "unknown")
    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        Config.validate()
