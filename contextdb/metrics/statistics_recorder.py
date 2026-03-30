import time
from statistics import mean


class StatisticsRecorder:
    """Collects LLM call counts, token usage, and latency."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0
        self._latencies_s = []

    def record(self, usage, latency_s: float):
        """Record one LLM call using usage dict and elapsed seconds."""
        self.calls += 1
        self.input_tokens += int(usage["input_tokens"])
        self.output_tokens += int(usage["output_tokens"])
        self.total_tokens += int(usage.get("total_tokens", usage["input_tokens"] + usage["output_tokens"]))
        # Anthropic cache metrics
        self.cache_creation_tokens += int(usage.get("cache_creation_input_tokens", 0))
        self.cache_read_tokens += int(usage.get("cache_read_input_tokens", 0))
        self._latencies_s.append(latency_s)

    def summary(self):
        """Return aggregated stats for reporting."""
        mean_latency_ms = mean(self._latencies_s) * 1000 if self._latencies_s else 0.0
        total_latency_ms = sum(self._latencies_s) * 1000
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "mean_latency_ms": mean_latency_ms,
            "total_latency_ms": total_latency_ms,
        }

    def format(self, title: str = "LLM Statistics") -> str:
        """Return formatted statistics string."""
        s = self.summary()
        total_s = s["total_latency_ms"] / 1000
        mins, secs = divmod(int(total_s), 60)
        lines = [
            f"*** {title} ***",
            f"    LLM calls: {s['calls']}",
            f"    Input tokens: {s['input_tokens']}",
            f"    Output tokens: {s['output_tokens']}",
            f"    Total tokens: {s['total_tokens']}",
        ]
        # Add cache info if present
        if s["cache_creation_tokens"] > 0 or s["cache_read_tokens"] > 0:
            lines.append(f"    Cache creation: {s['cache_creation_tokens']} tokens")
            lines.append(f"    Cache read: {s['cache_read_tokens']} tokens")
        lines.extend([
            f"    Mean latency: {s['mean_latency_ms']:.0f}ms",
            f"    Total latency: {mins:02d}:{secs:02d}",
        ])
        return "\n".join(lines)


class LLMWithStats:
    """LLM wrapper that records usage + latency for each chat call."""

    def __init__(self, llm, recorder: StatisticsRecorder):
        self.llm = llm
        self.recorder = recorder
        # Forward provider/model attributes for config lookup
        self.provider = getattr(llm, "provider", None)
        self.model = getattr(llm, "model", None)

    def chat(self, messages, system: str = "", tools=None, cache_key: str = None):
        start = time.perf_counter()
        resp = self.llm.chat(messages, system=system, tools=tools, cache_key=cache_key)
        latency_s = time.perf_counter() - start
        usage = resp.get("usage")
        if usage is None:
            raise ValueError("LLM response missing usage for statistics")
        self.recorder.record(usage, latency_s)
        return resp

    def chat_with_cache(
        self,
        messages,
        system: str = "",
        tools=None,
        cache_content: str = None,
        non_cached_content: str = None,
        cache_key: str = None,
    ):
        """Chat with Anthropic prompt caching support."""
        start = time.perf_counter()
        if hasattr(self.llm, "chat_with_cache"):
            resp = self.llm.chat_with_cache(
                messages,
                system=system,
                tools=tools,
                cache_content=cache_content,
                non_cached_content=non_cached_content,
                cache_key=cache_key,
            )
        else:
            # Fallback
            resp = self.llm.chat(messages, system=system, tools=tools, cache_key=cache_key)
        latency_s = time.perf_counter() - start
        usage = resp.get("usage")
        if usage is None:
            raise ValueError("LLM response missing usage for statistics")
        self.recorder.record(usage, latency_s)
        return resp
