import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contextdb.config import Config


def test_config():
    print("Testing LLM Configuration")
    print("=" * 50)

    try:
        Config.info()
        Config.validate()
        print("Config validated")

        client = Config.get_llm_client()
        print(f"LLM client: {type(client).__name__}")

        response = client.chat(
            messages=[{"role": "user", "content": "Say 'ready'"}]
        )
        text = response["content"][0].get("text", "")
        print(f"API Response: {text}")

        print("=" * 50)
        print("All tests passed")
        return True

    except Exception as e:
        print(f"Test failed: {e}")
        return False


if __name__ == "__main__":
    sys.exit(0 if test_config() else 1)
