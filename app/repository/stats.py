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
