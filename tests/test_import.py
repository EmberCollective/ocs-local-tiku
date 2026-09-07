"""批量导入：同键 upsert 计数、坏行跳过不中断、超 5000 拒绝、导入即命中。"""

import sqlite3

import pytest

from app.answer.service import AnswerService
from app.normalize import build_cache_key
from app.repository import questions, stats
from tests.conftest import FakeLLM

pytestmark = pytest.mark.asyncio

TITLE = "下列哪项是中国的首都"
OPTIONS_RAW = "A. 北京\nB. 上海"


def _item(question=TITLE, type="single", options=OPTIONS_RAW, answer="北京") -> dict:
    return {"question": question, "type": type, "options": options, "answer": answer}


# ---------- 仓储层 upsert_import ----------


async def test_upsert_import_inserts_new_rows(db):
    imported, updated, skipped = await questions.upsert_import(
        db, [_item(), _item(question="一加一等于几", type="completion", answer="2")]
    )
    assert (imported, updated, skipped) == (2, 0, 0)
    record = await questions.get_by_key(
        db, build_cache_key(TITLE, "single", ["A. 北京", "B. 上海"])
    )
    assert record is not None
    assert record.source == "import"
    assert record.options == ["A. 北京", "B. 上海"]
    assert record.answer == "北京"


async def test_upsert_import_updates_existing_key(db):
    await questions.upsert_import(db, [_item(answer="北京")])
    imported, updated, skipped = await questions.upsert_import(db, [_item(answer="上海")])
    assert (imported, updated, skipped) == (0, 1, 0)
    row = await db.query_one(
        "SELECT answer, source FROM questions WHERE question = ?", (TITLE,)
    )
    assert row["answer"] == "上海"  # UPDATE 生效
    assert row["source"] == "import"


async def test_upsert_import_duplicate_key_within_batch(db):
    imported, updated, skipped = await questions.upsert_import(db, [_item(), _item()])
    assert (imported, updated, skipped) == (1, 1, 0)
    row = await db.query_one("SELECT COUNT(*) AS n FROM questions")
    assert row["n"] == 1


async def test_upsert_import_skips_bad_rows_without_aborting(db):
    items = [
        {"question": "缺答案"},                      # 无 answer
        {"answer": "甲"},                            # 无 question
        _item(question="  ", answer="  "),           # 全空白
        "not-a-mapping",                            # 非对象行（防御）
        _item(),                                    # 好行
    ]
    imported, updated, skipped = await questions.upsert_import(db, items)
    assert (imported, updated, skipped) == (1, 0, 4)


async def test_upsert_import_options_accepts_string_and_list(db):
    from_str, _, _ = await questions.upsert_import(db, [_item(options=OPTIONS_RAW)])
    assert from_str == 1
    row = await db.query_one("SELECT options_json FROM questions WHERE question = ?", (TITLE,))
    assert row["options_json"] == '["A. 北京", "B. 上海"]'  # str 按分隔符切分

    imported, updated, _ = await questions.upsert_import(
        db, [_item(question="复数形式", options=["北京", "上海"])]
    )
    assert (imported, updated) == (1, 0)
    row = await db.query_one("SELECT options_json FROM questions WHERE question = '复数形式'")
    assert row["options_json"] == '["北京", "上海"]'  # list 原样保留


async def test_upsert_import_judgement_ignores_options_in_key(db):
    await questions.upsert_import(db, [_item(question="地球是圆的", type="judgement", options=["对", "错"], answer="正确")])
    imported, updated, _ = await questions.upsert_import(
        db, [_item(question="地球是圆的", type="judgement", options=None, answer="正确")]
    )
    assert (imported, updated) == (0, 1)  # 判断题选项不参与键 → 同键 UPDATE


async def test_upsert_import_invalid_type_falls_back_to_single(db):
    await questions.upsert_import(db, [_item(type="essay")])
    row = await db.query_one("SELECT qtype FROM questions WHERE question = ?", (TITLE,))
    assert row["qtype"] == "single"


async def test_upsert_import_skips_illegal_options_types(db):
    """options 为非字符串类型或混入非字符串元素 → 整行跳过，不影响其他行。"""
    imported, _, skipped = await questions.upsert_import(
        db, [_item(question="选项是数字", options=123), _item(question="列表混入数字", options=["甲", 5])]
    )
    assert (imported, skipped) == (0, 2)


async def test_upsert_import_row_db_error_counts_skipped(db, monkeypatch):
    """单行 DB 异常只跳过该行，不中断整批（事务内继续）。"""
    original_insert = db.insert
    has_failed = False

    async def flaky_insert(sql: str, params: tuple = ()):
        nonlocal has_failed
        if not has_failed and sql.strip().startswith("INSERT INTO questions"):
            has_failed = True
            raise sqlite3.OperationalError("database is locked")
        return await original_insert(sql, params)

    monkeypatch.setattr(db, "insert", flaky_insert)
    imported, _, skipped = await questions.upsert_import(db, [_item(), _item(question="第二题")])
    assert (imported, skipped) == (1, 1)


# ---------- API 层 /api/import ----------


async def test_api_import_counts_and_logs(client):
    resp = await client.post(
        "/api/import",
        json={"items": [_item(), _item(question="下列哪项不是水果", options="A. 苹果\nB. 汽车", answer="汽车")]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"code": 1, "imported": 2, "updated": 0, "skipped": 0}

    again = (await client.post("/api/import", json={"items": [_item()]})).json()
    assert again == {"code": 1, "imported": 0, "updated": 1, "skipped": 0}

    logs = await client.app.state.db.query("SELECT kind, error FROM call_log WHERE kind = 'import'")
    assert len(logs) == 2  # 每批一条，成功 error=None
    assert all(log["error"] is None for log in logs)


async def test_api_import_skips_bad_rows_keeps_good(client):
    items = [_item(), {"question": "缺答案"}, {"answer": "甲"}, _item(question="  ", answer="  ")]
    resp = await client.post("/api/import", json={"items": items})
    assert resp.status_code == 200
    assert resp.json() == {"code": 1, "imported": 1, "updated": 0, "skipped": 3}
    rows = await client.app.state.db.query("SELECT COUNT(*) AS n FROM questions")
    assert rows[0]["n"] == 1  # 只有好行入库


async def test_api_import_rejects_over_5000(client):
    items = [_item(question=f"题目 {i}", answer="甲") for i in range(5001)]
    resp = await client.post("/api/import", json={"items": items})
    assert resp.status_code == 400
    db = client.app.state.db
    assert (await db.query_one("SELECT COUNT(*) AS n FROM questions"))["n"] == 0
    assert (await db.query_one("SELECT COUNT(*) AS n FROM call_log WHERE kind = 'import'"))["n"] == 0


async def test_imported_row_answers_query_without_llm(client):
    """导入后 /api/query 立即命中，LLM 零调用；str/list 两种 options 形态均可命中。"""
    await client.post(
        "/api/import",
        json={"items": [_item(options=OPTIONS_RAW), _item(question="复数形态题", options=["北京", "上海"], answer="上海")]},
    )

    fake = FakeLLM(reply="不应被调用")
    original = client.app.state.answer_service
    client.app.state.answer_service = AnswerService(client.app.state.db, fake)
    try:
        by_raw = (
            await client.get(
                "/api/query", params={"title": TITLE, "type": "single", "options": OPTIONS_RAW}
            )
        ).json()
        by_list = (
            await client.get(
                "/api/query", params={"title": "复数形态题", "type": "single", "options": "北京|上海"}
            )
        ).json()
    finally:
        client.app.state.answer_service = original

    assert by_raw["code"] == 1 and by_raw["answer"] == "A"
    assert by_list["code"] == 1 and by_list["answer"] == "B"
    assert fake.calls == 0  # 命中缓存，LLM 未被调用


async def test_api_import_internal_error_returns_500(client, monkeypatch):
    async def boom(database, items):
        raise RuntimeError("db down")

    monkeypatch.setattr(questions, "upsert_import", boom)
    resp = await client.post("/api/import", json={"items": [_item()]})
    assert resp.status_code == 500


async def test_api_import_log_failure_does_not_affect_response(client, monkeypatch):
    async def boom_log(database, **kwargs):
        raise RuntimeError("log down")

    monkeypatch.setattr(stats, "log_call", boom_log)
    resp = await client.post("/api/import", json={"items": [_item()]})
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
