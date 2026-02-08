import json
import os
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    def chat(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_key: str = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class LLMWithCacheProtocol(Protocol):
    """Extended protocol with prompt caching support."""

    def chat(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_key: str = None,
    ) -> dict[str, Any]: ...

    def chat_with_cache(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_content: str = None,
        non_cached_content: str = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        """Chat with prompt caching support for static content."""
        ...


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

    def chat(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, system, tools)
        elif self.provider == "openai":
            return self._chat_openai(messages, system, tools, cache_key=cache_key)

    def chat_with_cache(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_content: str = None,
        non_cached_content: str = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        """
        Chat with prompt caching support.

        For Anthropic, uses the cache_control feature to cache static content.
        Non-cached content is sent as regular input (no cache_control overhead).

        Args:
            messages: Chat messages
            system: System prompt
            tools: Tool definitions
            cache_content: Static content to cache (with cache_control, 1.25x write cost)
            non_cached_content: One-time content NOT worth caching (regular 1x input cost)

        Returns:
            Chat response with usage stats including cache metrics
        """
        if self.provider == "anthropic":
            return self._chat_anthropic_with_cache(messages, system, tools, cache_content, non_cached_content)
        else:
            # OpenAI doesn't have native prompt caching, fall back to regular chat
            parts = []
            if cache_content:
                parts.append(cache_content)
            if non_cached_content:
                parts.append(non_cached_content)
            if parts:
                messages = self._prepend_cache_content(messages, "\n\n".join(parts))
            return self._chat_openai(messages, system, tools, cache_key=cache_key)

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

    def _chat_anthropic_with_cache(
        self, messages: list[dict], system: str, tools: list[dict],
        cache_content: str, non_cached_content: str = None,
    ) -> dict[str, Any]:
        """
        Anthropic chat with prompt caching.

        User message structure:
        1. cache_content (with cache_control) — reusable prefix, 1.25x write / 0.1x read
        2. non_cached_content (NO cache_control) — one-time content, regular 1x cost
        3. dynamic prompt — query/beams/selected

        See: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
        """
        # Build messages with selective cache control
        cached_messages = []
        for i, msg in enumerate(messages):
            if i == 0 and msg["role"] == "user" and (cache_content or non_cached_content):
                dynamic = msg.get("content", "")
                content_blocks = []

                # Cached prefix (reusable across queries)
                if cache_content:
                    content_blocks.append({
                        "type": "text",
                        "text": cache_content,
                        "cache_control": {"type": "ephemeral"},
                    })

                # Non-cached content (one-time, no cache overhead)
                if non_cached_content:
                    content_blocks.append({"type": "text", "text": non_cached_content})

                # Dynamic prompt
                if isinstance(dynamic, str):
                    content_blocks.append({"type": "text", "text": dynamic})
                elif isinstance(dynamic, list):
                    content_blocks.extend(dynamic)

                cached_messages.append({"role": "user", "content": content_blocks})
            else:
                cached_messages.append(msg)

        kwargs = {"model": self.model, "max_tokens": 1024, "messages": cached_messages}

        # System prompt with cache_control (static across all calls)
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]

        # Tools with cache_control on last tool (static across all calls)
        if tools:
            cached_tools = []
            for j, tool in enumerate(tools):
                t = dict(tool)
                if j == len(tools) - 1:
                    # Mark last tool with cache_control (caches entire tools prefix)
                    t["cache_control"] = {"type": "ephemeral"}
                cached_tools.append(t)
            kwargs["tools"] = cached_tools

        response = self._client.messages.create(**kwargs)

        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            }

        result = {"content": [], "stop_reason": response.stop_reason, "usage": usage}
        for block in response.content:
            if block.type == "text":
                result["content"].append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result["content"].append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        return result

    def _prepend_cache_content(self, messages: list[dict], cache_content: str) -> list[dict]:
        """Prepend cache content to first user message."""
        if not messages:
            return [{"role": "user", "content": cache_content}]

        result = []
        prepended = False
        for msg in messages:
            if not prepended and msg["role"] == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    result.append({"role": "user", "content": f"{cache_content}\n\n{content}"})
                else:
                    result.append({"role": "user", "content": [{"type": "text", "text": cache_content}, *content]})
                prepended = True
            else:
                result.append(msg)
        return result

    def _chat_openai(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        cache_key: str = None,
    ) -> dict[str, Any]:
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
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key
            retention = os.getenv("OPENAI_PROMPT_CACHE_RETENTION")
            if retention:
                kwargs["prompt_cache_retention"] = retention
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
            prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            if prompt_details:
                if hasattr(prompt_details, "model_dump"):
                    details_dict = prompt_details.model_dump()
                elif isinstance(prompt_details, dict):
                    details_dict = prompt_details
                else:
                    details_dict = {}
                usage["prompt_tokens_details"] = details_dict
                usage["cached_tokens"] = int(details_dict.get("cached_tokens", 0) or 0)
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
