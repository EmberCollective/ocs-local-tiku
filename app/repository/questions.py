"""questions 读写仓储：touch、分页搜索、批量删、导出、批量导入。"""

import json
import logging
import sqlite3
import time
from collections.abc import Mapping, Sequence

from app.db import Database
from app.models import NewQuestion, QuestionRecord
from app.normalize import build_cache_key, canonical_qtype, normalize_text, split_options

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO questions (cache_key, question, qtype, options_json, answer, source)
VALUES (?, ?, ?, ?, ?, ?)
"""

_UPDATE_IMPORT_SQL = """
UPDATE questions SET question = ?, qtype = ?, options_json = ?, answer = ?
WHERE cache_key = ?
"""


async def get_by_key(db: Database, cache_key: str) -> QuestionRecord | None:
    row = await db.query_one("SELECT * FROM questions WHERE cache_key = ?", (cache_key,))
    return _to_record(row) if row is not None else None


async def insert_question(db: Database, record: NewQuestion) -> bool:
    """写入缓存；唯一约束天然去重。哈希已存在时比对规范化原文，不一致记碰撞日志。"""
    params = (
        record.cache_key,
        record.question,
        record.qtype,
        _dump_options(record.options),
        record.answer,
        record.source,
    )
    try:
        return await db.insert(_INSERT_SQL, params) is not None
    except sqlite3.IntegrityError:
        await _log_collision(db, record)
        return False


async def touch(db: Database, cache_key: str) -> None:
    """命中轻量更新：last_hit_at + hits 自增。"""
    await db.execute(
        "UPDATE questions SET last_hit_at = ?, hits = hits + 1 WHERE cache_key = ?",
        (int(time.time()), cache_key),
    )


async def list_page(
    db: Database,
    *,
    q: str | None,
    qtype: str | None,
    page: int,
    page_size: int,
) -> tuple[list[QuestionRecord], int]:
    """分页浏览 + 题目 LIKE 搜索 + 题型筛选，新题在前。"""
    where, params = _build_where(q, qtype)
    total_row = await db.query_one(f"SELECT COUNT(*) AS n FROM questions{where}", tuple(params))
    rows = await db.query(
        f"SELECT * FROM questions{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    )
    records = [_to_record(row) for row in rows]
    return records, (total_row["n"] if total_row else 0)


async def delete_by_ids(db: Database, ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    return await db.execute(f"DELETE FROM questions WHERE id IN ({placeholders})", tuple(ids))


async def upsert_import(db: Database, items: Sequence[object]) -> tuple[int, int, int]:
    """批量导入（单事务）：新键 INSERT（source='import'）、同键 UPDATE、坏行跳过不中断。

    返回 (imported, updated, skipped)。
    """
    imported = updated = skipped = 0
    async with db.transaction():
        for item in items:
            record = _prepare_import_item(item)
            if record is None:
                skipped += 1
                continue
            try:
                if await get_by_key(db, record.cache_key) is not None:
                    await _apply_import_update(db, record)
                    updated += 1
                else:
                    await db.insert(
                        _INSERT_SQL,
                        (record.cache_key, record.question, record.qtype,
                         _dump_options(record.options), record.answer, record.source),
                    )
                    imported += 1
            except Exception as error:
                skipped += 1
                logger.warning("导入行失败（跳过）：%r — %s", item, error)
    return imported, updated, skipped


def _prepare_import_item(item: object) -> NewQuestion | None:
    """单条导入归一：缺 question/answer、options 非法 → None（跳过）。"""
    if not isinstance(item, Mapping):
        return None
    question = item.get("question")
    answer = item.get("answer")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(answer, str) or not answer.strip():
        return None
    options = _import_options(item.get("options"))
    if options is None:
        return None
    qtype_raw = item.get("type")
    qtype = canonical_qtype(qtype_raw if isinstance(qtype_raw, str) else None)
    return NewQuestion(
        cache_key=build_cache_key(question, qtype, options),
        question=question.strip(), qtype=qtype, options=options,
        answer=answer.strip(), source="import",
    )


def _import_options(raw: object) -> list[str] | None:
    """options 兼容 str（按分隔符切分）与 list[str]（逐项清洗）；非法类型返回 None。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return split_options(raw)
    if isinstance(raw, list) and all(isinstance(option, str) for option in raw):
        return [option.strip() for option in raw if option.strip()]
    return None


async def _apply_import_update(db: Database, record: NewQuestion) -> None:
    await db.execute(
        _UPDATE_IMPORT_SQL,
        (record.question, record.qtype, _dump_options(record.options), record.answer,
         record.cache_key),
    )


def _dump_options(options: list[str]) -> str:
    return json.dumps(options, ensure_ascii=False)


async def export_all(db: Database) -> list[dict]:
    """导出全部缓存（备份），字段与导入条目对齐。"""
    rows = await db.query("SELECT * FROM questions ORDER BY id")
    return [
        {
            "question": row["question"],
            "type": row["qtype"],
            "options": json.loads(row["options_json"]),
            "answer": row["answer"],
        }
        for row in rows
    ]


async def count_all(db: Database) -> int:
    """缓存总条数（统计页「总条数」卡片）。"""
    row = await db.query_one("SELECT COUNT(*) AS n FROM questions")
    return row["n"] if row else 0


async def _log_collision(db: Database, record: NewQuestion) -> None:
    existing = await get_by_key(db, record.cache_key)
    if existing is None:
        return
    if normalize_text(existing.question) != normalize_text(record.question):
        logger.warning(
            "缓存键碰撞：%r 与 %r 哈希相同（cache_key=%s），保留首见答案",
            record.question,
            existing.question,
            record.cache_key,
        )


def _build_where(q: str | None, qtype: str | None) -> tuple[str, list]:
    conditions, params = [], []
    if q:
        conditions.append("question LIKE ?")
        params.append(f"%{q}%")
    if qtype:
        conditions.append("qtype = ?")
        params.append(qtype)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def _to_record(row: dict) -> QuestionRecord:
    return QuestionRecord(
        id=row["id"],
        cache_key=row["cache_key"],
        question=row["question"],
        qtype=row["qtype"],
        options=json.loads(row["options_json"]),
        answer=row["answer"],
        source=row["source"],
        created_at=row["created_at"],
        last_hit_at=row["last_hit_at"],
        hits=row["hits"],
    )
