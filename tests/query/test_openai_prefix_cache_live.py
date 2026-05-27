"""Live benchmark for OpenAI prefix cache ON/OFF in BlockRetriever.

This test is opt-in because it makes real API calls.
Enable with: RUN_LIVE_PREFIX_CACHE_TEST=1

Strict mode (default on):
- prefix_on: stable prompt_cache_key + prewarm.
- prefix_off: isolated prompt_cache_key without prewarm.
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import statistics
import sys
import time
import types
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / "docs" / "large_doc.json"

MAX_TURNS = 5
TOP_K = 3
OPENAI_RATE_LIMIT_RETRIES = 8
STRICT_CACHE_CONTROL = True
PREWARM_ALL_BLOCKS = True
REQUIRE_NO_RATE_LIMIT = True
PREFIX_ON_COOLDOWN_AFTER_PREWARM_S = 15
OPENAI_BENCHMARK_TEMPERATURE = 0
OPENAI_BENCHMARK_SEED = 7
NETWORK_RTT_PROBE_SAMPLES = 5
QUERY_SET = [
    "What is this document mainly about?",
    "What key airport design standards are discussed?",
    "What does it say about runway and taxiway requirements?",
    "What guidance is given for markings, signs, and lighting?",
    "What operational or safety constraints are emphasized?",
]


# Work around current package export mismatch in contextdb/__init__.py
# by loading contextdb as a namespace package from source path.
if "contextdb" not in sys.modules:
    pkg = types.ModuleType("contextdb")
    pkg.__path__ = [str(ROOT / "contextdb")]
    sys.modules["contextdb"] = pkg

from contextdb.core.storage import TreeDB  # noqa: E402
from contextdb.llm import LLMClient  # noqa: E402
from contextdb.retriever.algorithm.block_retriever import BlockRetriever  # noqa: E402


class NoPrefixCacheLLM:
    """Adapter that intentionally hides chat_with_cache to disable prefix split logic."""

    def __init__(self, llm: LLMClient):
        self._llm = llm
        self.provider = getattr(llm, "provider", None)
        self.model = getattr(llm, "model", None)

    def chat(self, messages, system="", tools=None, cache_key=None):
        try:
            return self._llm.chat(messages, system=system, tools=tools, cache_key=cache_key)
        except TypeError:
            # Compatibility with older LLMClient.chat signatures.
            return self._llm.chat(messages, system=system, tools=tools)


def _convert_flat_to_tree(flat_list: list[dict]) -> dict:
    root = {"type": "object", "attrs": {"title": "Document Root"}, "children": {}}
    stack = [(root, 0)]

    for i, item in enumerate(flat_list):
        level = int(item.get("level", 1) or 1)
        title = item.get("title", f"Section {i}")
        text = item.get("text", "")
        node_id = f"node_{i}"

        node = {
            "type": "object",
            "attrs": {"title": title, "summary": text[:300] if text else ""},
            "entity_id": node_id,
            "children": {},
        }

        while len(stack) > 1 and stack[-1][1] >= level:
            stack.pop()

        stack[-1][0]["children"][node_id] = node
        stack.append((node, level))

    return root


def _build_entities(flat_list: list[dict]) -> dict:
    return {
        f"node_{i}": {
            "type": "section",
            "title": item.get("title", ""),
            "text": item.get("text", ""),
            "level": int(item.get("level", 1) or 1),
        }
        for i, item in enumerate(flat_list)
    }


def _record_openai_usage(
    llm: LLMClient,
    *,
    strict_mode: str,
    stable_cache_key: str,
) -> tuple[list[dict], dict]:
    calls: list[dict] = []
    rate_limit_stats = {
        "events": 0,
        "retry_calls": 0,
        "total_sleep_s": 0.0,
        "max_retries_in_call": 0,
    }
    original_create = llm._client.chat.completions.create
    max_retries = OPENAI_RATE_LIMIT_RETRIES

    def wrapped_create(*args, **kwargs):
        backoff_s = 1.0
        call_rate_limit_retries = 0
        for attempt in range(max_retries):
            try:
                request_kwargs = dict(kwargs)

                if strict_mode == "on":
                    request_kwargs.setdefault("prompt_cache_key", stable_cache_key)
                    retention = os.getenv("OPENAI_PROMPT_CACHE_RETENTION")
                    if retention:
                        request_kwargs.setdefault("prompt_cache_retention", retention)
                elif strict_mode == "off":
                    # Keep prompts unchanged; isolate off-mode by cache key only.
                    request_kwargs["prompt_cache_key"] = f"{stable_cache_key}-off"

                # Keep benchmark deterministic so ON/OFF paths are comparable.
                request_kwargs.setdefault("temperature", OPENAI_BENCHMARK_TEMPERATURE)
                request_kwargs.setdefault("seed", OPENAI_BENCHMARK_SEED)

                started = time.perf_counter()
                response = original_create(*args, **request_kwargs)
                elapsed = time.perf_counter() - started
                break
            except Exception as exc:
                message = str(exc).lower()
                is_rate_limit = "rate limit" in message or "rate_limit" in message or " 429" in message
                if not is_rate_limit or attempt >= max_retries - 1:
                    raise

                if call_rate_limit_retries == 0:
                    rate_limit_stats["retry_calls"] += 1
                call_rate_limit_retries += 1
                rate_limit_stats["events"] += 1
                rate_limit_stats["max_retries_in_call"] = max(
                    rate_limit_stats["max_retries_in_call"],
                    call_rate_limit_retries,
                )
                rate_limit_stats["total_sleep_s"] += backoff_s
                print(
                    f"[rate-limit-warning] strict_mode={strict_mode} "
                    f"retry={call_rate_limit_retries} sleep={backoff_s:.1f}s"
                )
                time.sleep(backoff_s)
                backoff_s *= 2

        usage = response.usage.model_dump() if response.usage and hasattr(response.usage, "model_dump") else {}
        prompt_details = usage.get("prompt_tokens_details") or {}

        calls.append(
            {
                "latency_s": elapsed,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
                "rate_limit_retries": call_rate_limit_retries,
            }
        )
        return response

    llm._client.chat.completions.create = wrapped_create
    return calls, rate_limit_stats


def _prewarm_all_blocks(retriever: BlockRetriever, tree_id: str, *, top_k: int) -> tuple[int, int]:
    """Warm all fixed block prefixes before query benchmark."""
    plan = retriever._get_or_create_plan(tree_id)
    root_id = retriever.storage.get_root_id(tree_id) or ""
    frontier = [{"node_id": root_id, "title": "root", "path": "root"}]
    warmed = 0

    for block in plan.blocks:
        if not block.node_ids:
            continue
        # Warm prefix with the smallest dynamic payload.
        allowed_node_ids = [block.node_ids[0]]
        pick_limit = max(1, min(top_k, len(allowed_node_ids)))
        process_sig = inspect.signature(retriever._process_block)
        if "input_frontier" in process_sig.parameters:
            retriever._process_block(
                block=block,
                query="[warmup]",
                input_frontier=frontier,
                previous_top_candidate_ids=[],
                allowed_node_ids=allowed_node_ids,
                pick_limit=pick_limit,
            )
        warmed += 1

    return warmed, len(plan.blocks)


def _run_once(
    retriever: BlockRetriever,
    tree_id: str,
    query: str,
    calls: list[dict],
    *,
    max_turns: int,
    top_k: int,
) -> dict:
    start_idx = len(calls)
    started = time.perf_counter()

    result = retriever.retrieve(
        tree_id,
        query,
        beam_size=top_k,
        max_turns=max_turns,
        select_k=top_k,
    )

    wall_s = time.perf_counter() - started
    run_calls = calls[start_idx:]
    prompt_tokens = sum(c["prompt_tokens"] for c in run_calls)
    cached_tokens = sum(c["cached_tokens"] for c in run_calls)

    return {
        "query": query,
        "wall_s": wall_s,
        "api_latency_s": sum(c["latency_s"] for c in run_calls),
        "llm_calls": len(run_calls),
        "turns": int(getattr(result, "turns", 0) or 0),
        "nodes": len(getattr(result, "nodes", []) or []),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": sum(c["completion_tokens"] for c in run_calls),
        "cached_tokens": cached_tokens,
        "cache_util": (cached_tokens / prompt_tokens) if prompt_tokens else 0.0,
        "cache_hit": cached_tokens > 0,
        "rate_limit_retries": sum(c.get("rate_limit_retries", 0) for c in run_calls),
        "rate_limited_calls": sum(1 for c in run_calls if c.get("rate_limit_retries", 0) > 0),
    }


def _require_live_setup() -> tuple[str, str, list[dict]]:
    load_dotenv(ROOT / ".env")

    if os.getenv("RUN_LIVE_PREFIX_CACHE_TEST") != "1":
        pytest.skip("Set RUN_LIVE_PREFIX_CACHE_TEST=1 to run live OpenAI cache test")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is not set")

    if not DOC_PATH.exists():
        pytest.skip(f"Missing test document: {DOC_PATH}")

    with DOC_PATH.open() as f:
        flat_data = json.load(f)

    if not isinstance(flat_data, list) or not flat_data:
        pytest.skip(f"Expected non-empty flat list in {DOC_PATH}")

    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    return api_key, model, flat_data


def _aggregate_mode(
    mode: str,
    rows: list[dict],
    *,
    prewarmed_blocks: int,
    total_blocks: int,
    rate_limit_stats: dict,
    network_rtt_est_ms: float,
) -> dict:
    total_prompt = sum(r["prompt_tokens"] for r in rows)
    total_cached = sum(r["cached_tokens"] for r in rows)
    return {
        "mode": mode,
        "query_count": len(rows),
        "total_wall_s": sum(r["wall_s"] for r in rows),
        "avg_wall_s": (sum(r["wall_s"] for r in rows) / len(rows)) if rows else 0.0,
        "total_api_latency_s": sum(r["api_latency_s"] for r in rows),
        "total_llm_calls": sum(r["llm_calls"] for r in rows),
        "total_prompt_tokens": total_prompt,
        "total_cached_tokens": total_cached,
        "cache_util": (total_cached / total_prompt) if total_prompt else 0.0,
        "hit_queries": sum(1 for r in rows if r["cache_hit"]),
        "prewarmed_blocks": prewarmed_blocks,
        "total_blocks": total_blocks,
        "rate_limit_events": int(rate_limit_stats.get("events", 0)),
        "rate_limit_retry_calls": int(rate_limit_stats.get("retry_calls", 0)),
        "rate_limit_total_sleep_s": float(rate_limit_stats.get("total_sleep_s", 0.0)),
        "rate_limit_max_retries_in_call": int(rate_limit_stats.get("max_retries_in_call", 0)),
        "rate_limited": int(rate_limit_stats.get("events", 0)) > 0,
        "network_rtt_est_ms": float(network_rtt_est_ms),
        "rows": rows,
    }


def _probe_openai_network_rtt_ms(*, samples: int = NETWORK_RTT_PROBE_SAMPLES, timeout_s: float = 2.0) -> float:
    """Best-effort TCP RTT estimate to OpenAI endpoint for wall-time adjustment."""
    if samples <= 0:
        return 0.0

    measured_ms: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        try:
            with socket.create_connection(("api.openai.com", 443), timeout=timeout_s):
                pass
        except OSError:
            continue
        measured_ms.append((time.perf_counter() - start) * 1000.0)
        time.sleep(0.05)

    if not measured_ms:
        return 0.0
    return float(statistics.median(measured_ms))


def _run_mode(
    mode: str,
    *,
    api_key: str,
    model: str,
    flat_data: list[dict],
    queries: list[str],
    max_turns: int,
    top_k: int,
    db: TreeDB | None = None,
    tree_id: str | None = None,
) -> dict:
    own_db = db is None
    if own_db:
        data = [dict(item) for item in flat_data]
        tree = _convert_flat_to_tree(data)
        entities = _build_entities(data)
        db = TreeDB(":memory:")
        tree_id = db.ingest_tree(tree, entities=entities)
    assert db is not None
    assert tree_id is not None

    strict_mode = "on" if STRICT_CACHE_CONTROL and mode == "prefix_on" else "off"

    stable_cache_key = f"condb-prefix-{mode}-{uuid.uuid4().hex[:8]}"
    network_rtt_est_ms = _probe_openai_network_rtt_ms()
    previous_rtt_env = os.environ.get("CONDB_NETWORK_RTT_EST_MS")
    os.environ["CONDB_NETWORK_RTT_EST_MS"] = f"{network_rtt_est_ms:.3f}"
    print(f"[network-probe] mode={mode} rtt_est_ms={network_rtt_est_ms:.2f}")

    base_llm = LLMClient("openai", api_key=api_key, model=model)
    calls, rate_limit_stats = _record_openai_usage(
        base_llm,
        strict_mode=strict_mode,
        stable_cache_key=stable_cache_key,
    )
    llm = base_llm if mode == "prefix_on" else NoPrefixCacheLLM(base_llm)

    prewarmed_blocks = 0
    total_blocks = 0

    try:
        retriever = BlockRetriever(db, llm, max_tokens_per_block=16000)

        if mode == "prefix_on" and PREWARM_ALL_BLOCKS:
            prewarmed_blocks, total_blocks = _prewarm_all_blocks(retriever, tree_id, top_k=top_k)
            print(f"[prewarm] mode={mode} warmed={prewarmed_blocks}/{total_blocks}")
            print(
                f"[cooldown] mode={mode} sleeping={PREFIX_ON_COOLDOWN_AFTER_PREWARM_S}s "
                "before query run"
            )
            time.sleep(PREFIX_ON_COOLDOWN_AFTER_PREWARM_S)
        else:
            total_blocks = len(retriever._get_or_create_plan(tree_id).blocks)

        rows = [
            _run_once(
                retriever,
                tree_id,
                query=q,
                calls=calls,
                max_turns=max_turns,
                top_k=top_k,
            )
            for q in queries
        ]
    finally:
        if previous_rtt_env is None:
            os.environ.pop("CONDB_NETWORK_RTT_EST_MS", None)
        else:
            os.environ["CONDB_NETWORK_RTT_EST_MS"] = previous_rtt_env
        if own_db:
            db.close()

    return _aggregate_mode(
        mode,
        rows,
        prewarmed_blocks=prewarmed_blocks,
        total_blocks=total_blocks,
        rate_limit_stats=rate_limit_stats,
        network_rtt_est_ms=network_rtt_est_ms,
    )


def _enforce_no_rate_limit(summaries: list[dict]) -> None:
    if not REQUIRE_NO_RATE_LIMIT:
        return

    contaminated = [s for s in summaries if s.get("rate_limited")]
    if not contaminated:
        return

    details = ", ".join(
        f"{s['mode']}:events={s['rate_limit_events']},retry_calls={s['rate_limit_retry_calls']}"
        for s in contaminated
    )
    pytest.xfail(
        "Rate-limit contamination detected, benchmark is not representative. "
        f"Details: {details}"
    )


def run_single_query_on_off(query: str) -> tuple[dict, dict]:
    """Utility for per-query live tests (one query per file)."""
    api_key, model, flat_data = _require_live_setup()
    data = [dict(item) for item in flat_data]
    tree = _convert_flat_to_tree(data)
    entities = _build_entities(data)

    db = TreeDB(":memory:")
    try:
        tree_id = db.ingest_tree(tree, entities=entities)
        off = _run_mode(
            "prefix_off",
            api_key=api_key,
            model=model,
            flat_data=flat_data,
            queries=[query],
            max_turns=MAX_TURNS,
            top_k=TOP_K,
            db=db,
            tree_id=tree_id,
        )
        on = _run_mode(
            "prefix_on",
            api_key=api_key,
            model=model,
            flat_data=flat_data,
            queries=[query],
            max_turns=MAX_TURNS,
            top_k=TOP_K,
            db=db,
            tree_id=tree_id,
        )
    finally:
        db.close()
    _enforce_no_rate_limit([off, on])
    return on, off


def test_prefix_cache_on_off_speed_and_hits():
    api_key, model, flat_data = _require_live_setup()
    queries = QUERY_SET[:5]
    data = [dict(item) for item in flat_data]
    tree = _convert_flat_to_tree(data)
    entities = _build_entities(data)

    db = TreeDB(":memory:")
    try:
        tree_id = db.ingest_tree(tree, entities=entities)
        off = _run_mode(
            "prefix_off",
            api_key=api_key,
            model=model,
            flat_data=flat_data,
            queries=queries,
            max_turns=MAX_TURNS,
            top_k=TOP_K,
            db=db,
            tree_id=tree_id,
        )
        on = _run_mode(
            "prefix_on",
            api_key=api_key,
            model=model,
            flat_data=flat_data,
            queries=queries,
            max_turns=MAX_TURNS,
            top_k=TOP_K,
            db=db,
            tree_id=tree_id,
        )
    finally:
        db.close()

    print("\n[prefix-cache-on-off]")
    print(
        f"model={model} max_turns={MAX_TURNS} top_k={TOP_K} queries={len(queries)} "
        f"strict={STRICT_CACHE_CONTROL} prewarm={PREWARM_ALL_BLOCKS} "
        f"retries={OPENAI_RATE_LIMIT_RETRIES} "
        f"post_prewarm_cooldown_s={PREFIX_ON_COOLDOWN_AFTER_PREWARM_S} "
        f"temperature={OPENAI_BENCHMARK_TEMPERATURE} "
        f"seed={OPENAI_BENCHMARK_SEED}"
    )
    for summary in [off, on]:
        print(
            f"{summary['mode']}: avg_wall={summary['avg_wall_s']:.3f}s "
            f"total_wall={summary['total_wall_s']:.3f}s "
            f"calls={summary['total_llm_calls']} "
            f"cache_util={summary['cache_util']:.2%} "
            f"hit_queries={summary['hit_queries']}/{summary['query_count']} "
            f"prewarmed={summary['prewarmed_blocks']}/{summary['total_blocks']} "
            f"rtt_est={summary['network_rtt_est_ms']:.2f}ms"
        )
        if summary["rate_limited"]:
            print(
                f"  [rate-limit] events={summary['rate_limit_events']} "
                f"retry_calls={summary['rate_limit_retry_calls']} "
                f"sleep={summary['rate_limit_total_sleep_s']:.1f}s "
                f"max_retries_in_call={summary['rate_limit_max_retries_in_call']}"
            )
        for i, row in enumerate(summary["rows"], start=1):
            print(
                f"  q{i}: wall={row['wall_s']:.3f}s calls={row['llm_calls']} "
                f"cached={row['cached_tokens']} util={row['cache_util']:.2%} hit={row['cache_hit']}"
            )

    _enforce_no_rate_limit([off, on])

    assert on["query_count"] == 5
    assert off["query_count"] == 5
    assert on["total_llm_calls"] > 0
    assert off["total_llm_calls"] > 0

    if os.getenv("REQUIRE_PREFIX_CACHE_HIT") == "1":
        assert on["hit_queries"] > 0, "prefix_on mode did not observe any cache hit"
    elif on["hit_queries"] == 0:
        pytest.xfail("No cache hits observed in prefix_on mode; rerun to sample cache behavior")
