"""questions 仓储：读写、touch、分页搜索、删除、唯一键去重与碰撞兜底。"""

import time

import pytest

from app.models import NewQuestion
from app.repository import questions

pytestmark = pytest.mark.asyncio


def make_record(cache_key="k1", question="题目一", qtype="single", options=("甲", "乙"), answer="甲") -> NewQuestion:
    return NewQuestion(
        cache_key=cache_key,
        question=question,
        qtype=qtype,
        options=list(options),
        answer=answer,
    )


async def test_insert_and_get_roundtrip(db):
    await questions.insert_question(db, make_record())
    record = await questions.get_by_key(db, "k1")
    assert record is not None
    assert record.question == "题目一"
    assert record.qtype == "single"
    assert record.options == ["甲", "乙"]
    assert record.answer == "甲"
    assert record.source == "llm"
    assert record.hits == 0


async def test_get_by_key_missing_returns_none(db):
    assert await questions.get_by_key(db, "nope") is None


async def test_duplicate_key_insert_returns_false(db, caplog):
    assert await questions.insert_question(db, make_record()) is True
    assert await questions.insert_question(db, make_record()) is False
    rows = await db.query("SELECT COUNT(*) AS n FROM questions")
    assert rows[0]["n"] == 1


async def test_duplicate_key_with_different_question_logs_warning(db, caplog):
    await questions.insert_question(db, make_record(question="题目一"))
    with caplog.at_level("WARNING", logger="app.repository.questions"):
        await questions.insert_question(db, make_record(question="完全不同的题目"))
    assert "碰撞" in caplog.text or "collision" in caplog.text.lower()


async def test_touch_updates_hits_and_last_hit(db):
    await questions.insert_question(db, make_record())
    before = await questions.get_by_key(db, "k1")
    time.sleep(1.1)  # epoch 秒粒度，确保 last_hit_at 变化
    await questions.touch(db, "k1")
    after = await questions.get_by_key(db, "k1")
    assert after.hits == before.hits + 1
    assert after.last_hit_at > before.last_hit_at


async def test_touch_missing_key_is_noop(db):
    await questions.touch(db, "missing")  # 不抛异常


async def test_list_page_search_and_filter(db):
    await questions.insert_question(db, make_record(cache_key="k1", question="马克思主义基本原理", qtype="single"))
    await questions.insert_question(db, make_record(cache_key="k2", question="高等数学极限", qtype="multiple", options=("甲",), answer="甲"))
    await questions.insert_question(db, make_record(cache_key="k3", question="线性代数矩阵", qtype="single", options=("甲",), answer="甲"))

    rows, total = await questions.list_page(db, q=None, qtype=None, page=1, page_size=2)
    assert total == 3
    assert len(rows) == 2

    rows, total = await questions.list_page(db, q="数学", qtype=None, page=1, page_size=10)
    assert total == 1
    assert rows[0].question == "高等数学极限"

    rows, total = await questions.list_page(db, q="代数", qtype=None, page=1, page_size=10)
    assert total == 1
    assert rows[0].question == "线性代数矩阵"

    rows, total = await questions.list_page(db, q=None, qtype="single", page=1, page_size=10)
    assert total == 2

    rows, total = await questions.list_page(db, q="代数", qtype="single", page=1, page_size=10)
    assert total == 1
    assert rows[0].question == "线性代数矩阵"


async def test_list_page_orders_newest_first(db):
    await questions.insert_question(db, make_record(cache_key="k1", question="旧题"))
    time.sleep(1.1)
    await questions.insert_question(db, make_record(cache_key="k2", question="新题", options=("甲",), answer="甲"))
    rows, _ = await questions.list_page(db, q=None, qtype=None, page=1, page_size=10)
    assert rows[0].question == "新题"


async def test_delete_by_ids(db):
    await questions.insert_question(db, make_record(cache_key="k1"))
    await questions.insert_question(db, make_record(cache_key="k2", question="题目二", options=("甲",), answer="甲"))
    deleted = await questions.delete_by_ids(db, [1, 2, 999])
    assert deleted == 2
    remaining = await db.query_one("SELECT COUNT(*) AS n FROM questions")
    assert remaining["n"] == 0


async def test_export_all(db):
    await questions.insert_question(db, make_record(cache_key="k1"))
    exported = await questions.export_all(db)
    assert len(exported) == 1
    assert exported[0]["question"] == "题目一"
    assert exported[0]["options"] == ["甲", "乙"]
    assert exported[0]["answer"] == "甲"
