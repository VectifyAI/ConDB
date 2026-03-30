from .test_openai_prefix_cache_live import QUERY_SET, run_single_query_on_off


def test_query_03_runway_taxiway_on_off():
    query = QUERY_SET[2]
    on, off = run_single_query_on_off(query)

    print("\n[single-query]")
    print(f"q={query}")
    print(
        f"prefix_on: wall={on['total_wall_s']:.3f}s calls={on['total_llm_calls']} "
        f"cache_util={on['cache_util']:.2%} cached={on['total_cached_tokens']}"
    )
    print(
        f"prefix_off: wall={off['total_wall_s']:.3f}s calls={off['total_llm_calls']} "
        f"cache_util={off['cache_util']:.2%} cached={off['total_cached_tokens']}"
    )

    assert on["query_count"] == 1
    assert off["query_count"] == 1
    assert on["total_llm_calls"] > 0
    assert off["total_llm_calls"] > 0
