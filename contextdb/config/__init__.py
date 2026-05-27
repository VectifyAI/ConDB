"""Configuration loader for ContextDB."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).parent

# Load .env file
env_path = CONFIG_DIR.parent.parent / ".env"
load_dotenv(env_path)


def _load_yaml(path: Path) -> dict:
    """Load a YAML file."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def get_defaults() -> dict:
    """Get default configuration."""
    return _load_yaml(CONFIG_DIR / "config.yaml")


def get_llm_config(provider: str, model: str) -> dict:
    """Get LLM model configuration.

    Args:
        provider: LLM provider (anthropic, openai)
        model: Model name

    Returns:
        Model configuration dict with context_limit, max_concurrent, etc.
    """
    defaults = get_defaults().get("llm", {})
    provider_config = _load_yaml(CONFIG_DIR / "llm" / f"{provider}.yaml")
    model_config = provider_config.get(model, {})

    return {**defaults, **model_config}


def get_retriever_config(retriever_type: str) -> dict:
    """Get retriever configuration.

    Args:
        retriever_type: Retriever type (beam, block)

    Returns:
        Retriever configuration dict
    """
    defaults = get_defaults().get("retriever", {})
    retriever_config = _load_yaml(CONFIG_DIR / "retriever" / f"{retriever_type}.yaml")

    return {**defaults, **retriever_config}


class Config:
    """Configuration: .env for keys, config.yaml for settings, env vars override."""

    # Keys — from .env only
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    # Settings — config.yaml, env vars override
    _cfg = get_defaults().get("llm", {})
    LLM_PROVIDER = os.getenv("LLM_PROVIDER") or _cfg.get("provider", "anthropic")
    LLM_MODEL = os.getenv("LLM_MODEL") or _cfg.get("model", "claude-sonnet-4-6")
    DB_PATH = os.getenv("DB_PATH", "context.sqlite")

    @classmethod
    def validate(cls):
        if not cls.ANTHROPIC_API_KEY and not cls.OPENAI_API_KEY:
            raise ValueError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY")
        return True

    @classmethod
    def get_llm_client(cls):
        from contextdb.llm import LLMClient

        cls.validate()
        if cls.LLM_PROVIDER == "anthropic" and cls.ANTHROPIC_API_KEY:
            return LLMClient("anthropic", cls.ANTHROPIC_API_KEY, cls.LLM_MODEL)
        elif cls.LLM_PROVIDER == "openai" and cls.OPENAI_API_KEY:
            return LLMClient("openai", cls.OPENAI_API_KEY, cls.LLM_MODEL)
        raise ValueError(f"Invalid provider: {cls.LLM_PROVIDER}")

    @classmethod
    def info(cls):
        print(f"Provider: {cls.LLM_PROVIDER}")
        print(f"Model: {cls.LLM_MODEL}")
        print(f"Database: {cls.DB_PATH}")
        print(f"Anthropic: {'set' if cls.ANTHROPIC_API_KEY else 'not set'}")
        print(f"OpenAI: {'set' if cls.OPENAI_API_KEY else 'not set'}")
