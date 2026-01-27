import os
from pathlib import Path

from dotenv import load_dotenv

from contextdb.llm import LLMClient

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    DB_PATH = os.getenv("DB_PATH", "context.sqlite")

    @classmethod
    def validate(cls):
        if not cls.ANTHROPIC_API_KEY and not cls.OPENAI_API_KEY:
            raise ValueError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY")
        return True

    @classmethod
    def get_llm_client(cls) -> LLMClient:
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
