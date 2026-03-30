# Tests Quickstart

Run from repo root.

## Prerequisites
- `OPENAI_API_KEY` set in `.env` (for live query tests)
- dependencies installed (`pip install -r requirements.txt`)

## Unit Tests
```bash
venv/bin/pytest -q
```

## Live Prefix-Cache Tests
Single query:
```bash
RUN_LIVE_PREFIX_CACHE_TEST=1 OPENAI_STRICT_CACHE_CONTROL=1 OPENAI_PREWARM_ALL_BLOCKS=1 OPENAI_RATE_LIMIT_RETRIES=8 venv/bin/pytest -q -s tests/query/test_query_01_overview_live.py
```

5-query benchmark:
```bash
RUN_LIVE_PREFIX_CACHE_TEST=1 OPENAI_STRICT_CACHE_CONTROL=1 OPENAI_PREWARM_ALL_BLOCKS=1 OPENAI_RATE_LIMIT_RETRIES=8 venv/bin/pytest -q -s tests/query/test_openai_prefix_cache_live.py
```
