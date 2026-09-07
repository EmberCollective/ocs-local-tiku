"""统计仓储：每日计数原子自增、provider 维度统计、调用日志。"""

import pytest

from app.repository import stats

pytestmark = pytest.mark.asyncio

DAY = "2026-09-07"


async def test_bump_daily_inserts_then_increments(db):
    await stats.bump_daily(db, DAY, cache_hits=1)
    await stats.bump_daily(db, DAY, cache_hits=2, llm_calls=1)
    row = await db.query_one("SELECT * FROM stats_daily WHERE day = ?", (DAY,))
    assert row["cache_hits"] == 3
    assert row["llm_calls"] == 1
    assert row["cache_misses"] == 0


async def test_bump_daily_supports_token_fields(db):
    await stats.bump_daily(db, DAY, prompt_tokens=100, completion_tokens=50)
    row = await db.query_one("SELECT * FROM stats_daily WHERE day = ?", (DAY,))
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50


async def test_bump_provider_daily_composite_key(db):
    await stats.bump_provider_daily(db, DAY, "p1", calls=1)
    await stats.bump_provider_daily(db, DAY, "p1", calls=1, failures=1)
    await stats.bump_provider_daily(db, DAY, "p2", calls=5)
    rows = {r["provider_id"]: r for r in await db.query("SELECT * FROM provider_stats_daily")}
    assert rows["p1"]["calls"] == 2
    assert rows["p1"]["failures"] == 1
    assert rows["p2"]["calls"] == 5


async def test_log_call_writes_all_fields(db):
    await stats.log_call(
        db,
        kind="llm-fail",
        cache_key="abc",
        question="题目",
        provider_id="p1",
        model="gpt-test",
        latency_ms=1234,
        prompt_tokens=10,
        completion_tokens=0,
        error="Connection error",
    )
    row = await db.query_one("SELECT * FROM call_log")
    assert row["kind"] == "llm-fail"
    assert row["cache_key"] == "abc"
    assert row["question"] == "题目"
    assert row["provider_id"] == "p1"
    assert row["model"] == "gpt-test"
    assert row["latency_ms"] == 1234
    assert row["prompt_tokens"] == 10
    assert row["error"] == "Connection error"


async def test_log_call_minimal_fields(db):
    await stats.log_call(db, kind="hit")
    row = await db.query_one("SELECT * FROM call_log")
    assert row["kind"] == "hit"
    assert row["question"] is None
    assert row["error"] is None


async def test_bump_daily_rejects_unknown_field(db):
    with pytest.raises(ValueError):
        await stats.bump_daily(db, DAY, not_a_field=1)
