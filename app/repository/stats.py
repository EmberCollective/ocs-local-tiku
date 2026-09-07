"""统计仓储：每日计数原子自增 + 调用日志。

统计表用 INSERT ... ON CONFLICT DO UPDATE SET x = x + excluded.x 原子自增；
day 由应用层按本地时区分桶（design §6）。
"""

import time

from app.db import Database

DAILY_FIELDS = (
    "cache_hits",
    "cache_misses",
    "llm_calls",
    "llm_failures",
    "prompt_tokens",
    "completion_tokens",
)
PROVIDER_FIELDS = ("calls", "failures", "prompt_tokens", "completion_tokens")

_CALL_LOG_OPTIONAL_COLUMNS = (
    "cache_key",
    "question",
    "provider_id",
    "model",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "error",
)


def today() -> str:
    """本地时区 YYYY-MM-DD（stats_daily 分桶键）。"""
    return time.strftime("%Y-%m-%d")


def days_ago(n: int) -> str:
    """n 天前的 YYYY-MM-DD（本地时区），供统计窗口/趋势序列使用。"""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


async def bump_daily(db: Database, day: str, **deltas: int) -> None:
    await _upsert_counters(db, "stats_daily", ("day",), (day,), DAILY_FIELDS, deltas)


async def bump_provider_daily(db: Database, day: str, provider_id: str, **deltas: int) -> None:
    await _upsert_counters(
        db, "provider_stats_daily", ("day", "provider_id"), (day, provider_id), PROVIDER_FIELDS, deltas
    )


async def log_call(
    db: Database,
    *,
    kind: str,
    cache_key: str | None = None,
    question: str | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error: str | None = None,
) -> None:
    """写一条调用日志；None 字段落库为 NULL。"""
    values = {
        "cache_key": cache_key,
        "question": question,
        "provider_id": provider_id,
        "model": model,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": error,
    }
    provided = {name: value for name, value in values.items() if value is not None}
    columns = ["kind", *provided.keys()]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO call_log ({', '.join(columns)}) VALUES ({placeholders})"
    await db.insert(sql, (kind, *provided.values()))


async def provider_stats_since(db: Database, since_day: str) -> dict[str, dict]:
    """近 24h 各 provider 聚合（day 粒度：昨日+今日），键为 provider_id。"""
    rows = await db.query(
        "SELECT provider_id, SUM(calls) AS calls, SUM(failures) AS failures,"
        " SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens"
        " FROM provider_stats_daily WHERE day >= ? GROUP BY provider_id",
        (since_day,),
    )
    return {row["provider_id"]: dict(row) for row in rows}


async def avg_latency_since(db: Database, since_ts: int) -> dict[str, float]:
    """近 24h call_log 各 provider 平均延迟（毫秒），键为 provider_id。"""
    rows = await db.query(
        "SELECT provider_id, AVG(latency_ms) AS avg_latency_ms FROM call_log"
        " WHERE provider_id IS NOT NULL AND ts >= ? GROUP BY provider_id",
        (since_ts,),
    )
    return {row["provider_id"]: row["avg_latency_ms"] for row in rows}


async def list_log(db: Database, *, limit: int, kind: str | None) -> list[dict]:
    """调用日志倒序（新→旧）；kind 可选过滤。"""
    if kind:
        return await db.query(
            "SELECT * FROM call_log WHERE kind = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (kind, limit),
        )
    return await db.query("SELECT * FROM call_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,))


async def daily_between(db: Database, start_day: str, end_day: str) -> list[dict]:
    """区间内每天的统计行（含 day 键），供趋势序列填充。"""
    return await db.query(
        "SELECT * FROM stats_daily WHERE day BETWEEN ? AND ? ORDER BY day",
        (start_day, end_day),
    )


async def daily_totals(db: Database) -> dict:
    """全量累计（各字段求和，空表补零）。"""
    columns = ", ".join(f"COALESCE(SUM({field}), 0) AS {field}" for field in DAILY_FIELDS)
    row = await db.query_one(f"SELECT {columns} FROM stats_daily")
    return dict(row) if row else {field: 0 for field in DAILY_FIELDS}


async def _upsert_counters(
    db: Database,
    table: str,
    key_columns: tuple[str, ...],
    key_values: tuple,
    counter_fields: tuple[str, ...],
    deltas: dict[str, int],
) -> None:
    unknown = set(deltas) - set(counter_fields)
    if unknown:
        raise ValueError(f"未知统计字段: {sorted(unknown)}")
    if not deltas:
        return
    columns = [*key_columns, *deltas.keys()]
    placeholders = ", ".join("?" for _ in columns)
    conflict_columns = ", ".join(key_columns)
    updates = ", ".join(f"{name} = {name} + excluded.{name}" for name in deltas)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_columns}) DO UPDATE SET {updates}"
    )
    await db.execute(sql, (*key_values, *deltas.values()))
