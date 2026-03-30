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
        cache_content: str | list[str] = None,
        non_cached_content: str = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        """Chat with prompt caching support for static content."""
        ...


# ── Helpers ────────────────────────────────────────────────────────

def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tools (input_schema) to OpenAI format (parameters)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _response_to_internal(response) -> dict[str, Any]:
    """Convert litellm ModelResponse (OpenAI format) to our internal format."""
    choice = response.choices[0]

    usage = None
    if hasattr(response, "usage") and response.usage:
        usage = {
            "input_tokens": response.usage.prompt_tokens or 0,
            "output_tokens": response.usage.completion_tokens or 0,
            "total_tokens": response.usage.total_tokens or 0,
        }
        # Anthropic cache stats (litellm passes these through)
        cache_creation = getattr(response.usage, "cache_creation_input_tokens", None)
        cache_read = getattr(response.usage, "cache_read_input_tokens", None)
        if cache_creation is not None:
            usage["cache_creation_input_tokens"] = cache_creation or 0
        if cache_read is not None:
            usage["cache_read_input_tokens"] = cache_read or 0
        # OpenAI cache stats
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

    # Map finish_reason to Anthropic-style stop_reason
    finish = choice.finish_reason
    stop_reason = {"stop": "end_turn", "tool_calls": "tool_use"}.get(finish, finish)

    result: dict[str, Any] = {"content": [], "stop_reason": stop_reason, "usage": usage}

    if choice.message.content:
        result["content"].append({"type": "text", "text": choice.message.content})
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            args = tc.function.arguments or ""
            try:
                parsed = json.loads(args) if args else {}
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid tool arguments JSON: {args}") from e
            result["content"].append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": parsed,
            })
    return result


def _convert_assistant_blocks(content: list) -> dict[str, Any]:
    """Convert Anthropic-style assistant content blocks to OpenAI format message."""
    tool_calls = []
    text_content = ""
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    },
                })
            elif block.get("type") == "text":
                text_content = block["text"]
        elif hasattr(block, "type"):
            if block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
            elif block.type == "text":
                text_content = block.text
    oai_msg: dict[str, Any] = {"role": "assistant", "content": text_content or None}
    if tool_calls:
        oai_msg["tool_calls"] = tool_calls
    return oai_msg


def _build_messages(messages: list[dict], system: str = "") -> list[dict]:
    """Build litellm-compatible message list (OpenAI format)."""
    out = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        if msg["role"] == "user":
            content = msg["content"]
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        out.append({
                            "role": "tool",
                            "tool_call_id": item["tool_use_id"],
                            "content": item["content"],
                        })
                    elif isinstance(item, dict) and item.get("type") == "text":
                        out.append({"role": "user", "content": item["text"]})
            else:
                out.append({"role": "user", "content": content})
        elif msg["role"] == "assistant":
            content = msg["content"]
            if isinstance(content, list):
                out.append(_convert_assistant_blocks(content))
            else:
                out.append({"role": "assistant", "content": content})
        else:
            out.append(msg)
    return out


# ── Default models ────────────────────────────────────────────────

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4",
}

_PROVIDER_PREFIXES = {
    "anthropic": "anthropic",
    "openai": "openai",
}


def _resolve_litellm_model(provider: str, model: str = None) -> str:
    """Resolve to a litellm model string like 'anthropic/claude-...'."""
    model = model or _DEFAULT_MODELS.get(provider)
    if not model:
        raise ValueError(f"No default model for provider {provider!r}. Pass model= explicitly.")

    # If user already included provider prefix, use as-is
    if "/" in model:
        return model

    prefix = _PROVIDER_PREFIXES.get(provider)
    if prefix:
        return f"{prefix}/{model}"
    return f"{provider}/{model}"


# ── LLMClient ─────────────────────────────────────────────────────

class LLMClient:
    def __init__(self, provider: str = "anthropic", api_key: str = None, model: str = None):
        import litellm
        self._litellm = litellm
        self.provider = provider
        self.model = _resolve_litellm_model(provider, model)
        self._api_key = api_key

        # Suppress litellm's verbose logging by default
        litellm.suppress_debug_info = True
        import logging
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)

    def _call(self, **kwargs) -> Any:
        """Central litellm.completion call, injects api_key if set."""
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return self._litellm.completion(**kwargs)

    def chat(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        oai_messages = _build_messages(messages, system)

        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": 1024, "messages": oai_messages}
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)

        response = self._call(**kwargs)
        return _response_to_internal(response)

    def chat_with_cache(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        cache_content: str | list[str] = None,
        non_cached_content: str = None,
        cache_key: str = None,
    ) -> dict[str, Any]:
        """Chat with prompt caching. Uses Anthropic cache_control when on Anthropic."""
        is_anthropic = "anthropic" in self.model or self.provider == "anthropic"

        if not is_anthropic:
            # Non-Anthropic: merge cache_content into messages, call normally
            parts = []
            if cache_content:
                parts.extend(cache_content if isinstance(cache_content, list) else [cache_content])
            if non_cached_content:
                parts.append(non_cached_content)
            if parts:
                messages = self._prepend_content(messages, "\n\n".join(p for p in parts if p))
            return self.chat(messages, system=system, tools=tools, cache_key=cache_key)

        # ── Anthropic prompt caching via cache_control ──

        # Merge segments if exceeding max cache_control blocks
        if isinstance(cache_content, list):
            segments = [s for s in cache_content if s]
            max_cache_controls = max(1, int(os.getenv("CONDB_ANTHROPIC_MAX_CACHE_CONTROL_BLOCKS", "4") or 4))
            reserved = (1 if system else 0) + (1 if tools else 0)
            allowed = max(1, max_cache_controls - reserved)
            if len(segments) > allowed:
                merge_count = len(segments) - allowed + 1
                merged_head = "\n\n".join(segments[:merge_count])
                segments = [merged_head, *segments[merge_count:]]
            cache_content = segments

        # Build first user message with cache_control blocks
        cached_messages = []
        for i, msg in enumerate(messages):
            if i == 0 and msg["role"] == "user" and (cache_content or non_cached_content):
                dynamic = msg.get("content", "")
                content_blocks = []

                if cache_content:
                    cc_list = cache_content if isinstance(cache_content, list) else [cache_content]
                    for segment in cc_list:
                        if segment:
                            content_blocks.append({
                                "type": "text",
                                "text": segment,
                                "cache_control": {"type": "ephemeral"},
                            })

                if non_cached_content:
                    content_blocks.append({"type": "text", "text": non_cached_content})

                if isinstance(dynamic, str) and dynamic:
                    content_blocks.append({"type": "text", "text": dynamic})
                elif isinstance(dynamic, list):
                    content_blocks.extend(dynamic)

                cached_messages.append({"role": "user", "content": content_blocks})
            else:
                cached_messages.append(msg)

        # Build tools with cache_control on last tool
        oai_tools = None
        if tools:
            oai_tools = _tools_to_openai(tools)
            if oai_tools:
                oai_tools[-1]["function"]["cache_control"] = {"type": "ephemeral"}

        # Convert remaining messages (assistant tool_calls, tool_results)
        # while preserving cache_control content blocks on the first user message
        final_messages = []
        if system:
            final_messages.append({
                "role": "system",
                "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            })

        for msg in cached_messages:
            if msg["role"] == "user":
                content = msg["content"]
                if isinstance(content, list):
                    # If blocks have cache_control, pass through as-is (litellm forwards to Anthropic)
                    has_cache_control = any(
                        isinstance(item, dict) and "cache_control" in item
                        for item in content
                    )
                    if has_cache_control:
                        final_messages.append({"role": "user", "content": content})
                    else:
                        # Regular list content — convert tool_results etc.
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result":
                                final_messages.append({
                                    "role": "tool",
                                    "tool_call_id": item["tool_use_id"],
                                    "content": item["content"],
                                })
                            elif isinstance(item, dict) and item.get("type") == "text":
                                final_messages.append({"role": "user", "content": item["text"]})
                else:
                    final_messages.append({"role": "user", "content": content})
            elif msg["role"] == "assistant":
                content = msg["content"]
                if isinstance(content, list):
                    final_messages.append(_convert_assistant_blocks(content))
                else:
                    final_messages.append({"role": "assistant", "content": content})
            else:
                final_messages.append(msg)

        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": 1024, "messages": final_messages}
        if oai_tools:
            kwargs["tools"] = oai_tools

        response = self._call(**kwargs)
        return _response_to_internal(response)

    @staticmethod
    def _prepend_content(messages: list[dict], content: str) -> list[dict]:
        """Prepend content to the first user message."""
        if not messages:
            return [{"role": "user", "content": content}]

        result = []
        prepended = False
        for msg in messages:
            if not prepended and msg["role"] == "user":
                existing = msg.get("content", "")
                if isinstance(existing, str):
                    result.append({"role": "user", "content": f"{content}\n\n{existing}"})
                else:
                    result.append({"role": "user", "content": [{"type": "text", "text": content}, *existing]})
                prepended = True
            else:
                result.append(msg)
        return result
