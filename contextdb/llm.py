import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    def chat(self, messages: list[dict], system: str = "", tools: list[dict] = None) -> dict[str, Any]: ...


class LLMClient:
    def __init__(self, provider: str = "anthropic", api_key: str = None, model: str = None):
        self.provider = provider
        self.model = model
        self._client = None

        if provider == "anthropic":
            from anthropic import Anthropic

            self._client = Anthropic(api_key=api_key)
            self.model = model or "claude-sonnet-4-20250514"
        elif provider == "openai":
            from openai import OpenAI

            self._client = OpenAI(api_key=api_key)
            self.model = model or "gpt-4"
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def chat(self, messages: list[dict], system: str = "", tools: list[dict] = None) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, system, tools)
        elif self.provider == "openai":
            return self._chat_openai(messages, system, tools)

    def _chat_anthropic(self, messages: list[dict], system: str, tools: list[dict]) -> dict[str, Any]:
        kwargs = {"model": self.model, "max_tokens": 1024, "messages": messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }
        result = {"content": [], "stop_reason": response.stop_reason, "usage": usage}
        for block in response.content:
            if block.type == "text":
                result["content"].append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result["content"].append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        return result

    def _chat_openai(self, messages: list[dict], system: str, tools: list[dict]) -> dict[str, Any]:
        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})

        for msg in messages:
            if msg["role"] == "user":
                content = msg["content"]
                if isinstance(content, list):
                    # tool results
                    for item in content:
                        if item.get("type") == "tool_result":
                            oai_messages.append(
                                {"role": "tool", "tool_call_id": item["tool_use_id"], "content": item["content"]}
                            )
                else:
                    oai_messages.append({"role": "user", "content": content})
            elif msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    # has tool calls
                    tool_calls = []
                    text_content = ""
                    for block in content:
                        # Handle both dict and object formats
                        if isinstance(block, dict):
                            block_type = block.get("type")
                            if block_type == "tool_use":
                                tool_calls.append(
                                    {
                                        "id": block["id"],
                                        "type": "function",
                                        "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                                    }
                                )
                            elif block_type == "text":
                                text_content = block["text"]
                        elif hasattr(block, "type"):
                            if block.type == "tool_use":
                                tool_calls.append(
                                    {
                                        "id": block.id,
                                        "type": "function",
                                        "function": {"name": block.name, "arguments": json.dumps(block.input)},
                                    }
                                )
                            elif block.type == "text":
                                text_content = block.text
                    oai_msg = {"role": "assistant", "content": text_content or None}
                    if tool_calls:
                        oai_msg["tool_calls"] = tool_calls
                    oai_messages.append(oai_msg)
                else:
                    oai_messages.append({"role": "assistant", "content": content})

        kwargs = {"model": self.model, "messages": oai_messages}
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
                }
                for t in tools
            ]

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        result = {"content": [], "stop_reason": choice.finish_reason, "usage": usage}
        if choice.message.content:
            result["content"].append({"type": "text", "text": choice.message.content})
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = tc.function.arguments or ""
                try:
                    parsed = json.loads(args) if args else {}
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid tool arguments JSON: {args}") from e
                result["content"].append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": parsed})
        return result
