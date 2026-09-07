"""查询主流程闭环：缓存命中/未命中、in-flight 去重、失败不缓存、先答后写。"""

import asyncio
import sqlite3

import pytest

from app.answer.service import AnswerService
from app.repository import questions, stats
from tests.conftest import FakeLLM

pytestmark = pytest.mark.asyncio

TITLE = "下列哪项是中国的首都"
OPTIONS_A = "A. 北京\nB. 上海"


@pytest.fixture
def service_factory(db):
    def make(reply: str = "北京", delay: float = 0.0, error: Exception | None = None):
        fake = FakeLLM(reply=reply, delay=delay, error=error)
        return AnswerService(db, fake), fake

    return make


async def test_miss_then_hit_calls_llm_once(service_factory):
    service, fake = service_factory(reply="北京")
    first = await service.query(TITLE, "single", OPTIONS_A)
    assert first.ok is True
    assert first.kind == "miss"
    assert first.answer == "A"

    second = await service.query(TITLE, "single", OPTIONS_A)
    assert second.ok is True
    assert second.kind == "hit"
    assert second.answer == "A"
    assert fake.calls == 1


async def test_inflight_dedup_merges_concurrent_same_question(service_factory):
    service, fake = service_factory(reply="北京", delay=0.05)
    outcomes = await asyncio.gather(
        *[service.query(TITLE, "single", OPTIONS_A) for _ in range(10)]
    )
    assert fake.calls == 1
    assert all(o.ok and o.answer == "A" for o in outcomes)


async def test_llm_failure_returns_code0_and_not_cached(service_factory):
    service, fake = service_factory(error=RuntimeError("connection refused"))
    outcome = await service.query(TITLE, "single", OPTIONS_A)
    assert outcome.ok is False
    assert outcome.kind == "llm-fail"
    assert "LLM" in outcome.msg

    again = await service.query(TITLE, "single", OPTIONS_A)
    assert again.kind == "llm-fail"  # 未缓存 → 再次真实尝试
    assert fake.calls == 2


async def test_parse_failure_not_cached(service_factory):
    service, fake = service_factory(reply="抱歉，我无法回答这个问题")
    outcome = await service.query(TITLE, "single", OPTIONS_A)
    assert outcome.ok is False
    assert outcome.kind == "parse-fail"

    again = await service.query(TITLE, "single", OPTIONS_A)
    assert again.kind == "parse-fail"
    assert fake.calls == 2


async def test_reordered_options_hit_cache_with_current_order_letters(service_factory):
    service, fake = service_factory(reply="北京")
    first = await service.query(TITLE, "single", "A. 北京\nB. 上海")
    assert first.answer == "A"

    second = await service.query(TITLE, "single", "B. 上海\nA. 北京")
    assert second.kind == "hit"
    assert second.answer == "B"  # 北京现在位于 B 位
    assert fake.calls == 1


async def test_hit_touches_cache_record(db, service_factory):
    service, _ = service_factory(reply="北京")
    await service.query(TITLE, "single", OPTIONS_A)
    await service.query(TITLE, "single", OPTIONS_A)
    record = await questions.get_by_key(
        db,
        (await db.query_one("SELECT cache_key FROM questions"))["cache_key"],
    )
    assert record.hits == 1


async def test_judgement_flow_ignores_options(service_factory):
    service, fake = service_factory(reply="正确")
    first = await service.query("地球是圆的", "judgement", "对\n错")
    assert first.ok and first.answer == "正确"
    second = await service.query("地球是圆的", "judgement", "正确\n错误")
    assert second.kind == "hit"
    assert second.answer == "正确"
    assert fake.calls == 1


async def test_stats_and_call_log_recorded(db, service_factory):
    service, _ = service_factory(reply="北京")
    await service.query(TITLE, "single", OPTIONS_A)
    await service.query(TITLE, "single", OPTIONS_A)

    row = await db.query_one("SELECT * FROM stats_daily WHERE day = ?", (stats.today(),))
    assert row["cache_misses"] == 1
    assert row["cache_hits"] == 1
    assert row["llm_calls"] == 1
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5

    kinds = {r["kind"] for r in await db.query("SELECT kind FROM call_log")}
    assert kinds == {"miss", "hit"}


async def test_cache_write_failure_does_not_break_answer(db, service_factory, monkeypatch):
    async def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(questions, "insert_question", boom)
    service, _ = service_factory(reply="北京")
    outcome = await service.query(TITLE, "single", OPTIONS_A)
    assert outcome.ok is True
    assert outcome.answer == "A"  # 先答后写：写入失败不影响返回


async def test_touch_failure_does_not_break_hit(db, service_factory, monkeypatch):
    service, _ = service_factory(reply="北京")
    await service.query(TITLE, "single", OPTIONS_A)

    async def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(questions, "touch", boom)
    outcome = await service.query(TITLE, "single", OPTIONS_A)
    assert outcome.ok is True
    assert outcome.kind == "hit"


async def test_type_falls_back_to_single(db, service_factory):
    from app.normalize import build_cache_key, split_options

    service, _ = service_factory(reply="北京")
    outcome = await service.query(TITLE, "essay", OPTIONS_A)
    assert outcome.ok is True
    key = build_cache_key(TITLE, "single", split_options(OPTIONS_A))
    record = await questions.get_by_key(db, key)
    assert record is not None
    assert record.qtype == "single"
