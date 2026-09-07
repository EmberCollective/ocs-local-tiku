"""aiosqlite 单连接封装：PRAGMA 调优 + user_version 版本化迁移 + SQLITE_BUSY 重试。

单连接由 aiosqlite 内部队列串行执行；WAL 使读写不互斥，容器/进程重启安全。
"""

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

logger = logging.getLogger(__name__)

BUSY_RETRIES = 1
BUSY_RETRY_DELAY_SECONDS = 0.1

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)

SCHEMA_V1 = """
CREATE TABLE questions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  cache_key    TEXT NOT NULL UNIQUE,
  question     TEXT NOT NULL,
  qtype        TEXT NOT NULL CHECK(qtype IN ('single','multiple','judgement','completion')),
  options_json TEXT NOT NULL DEFAULT '[]',
  answer       TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'llm' CHECK(source IN ('llm','import')),
  created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  last_hit_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  hits         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_questions_last_hit ON questions(last_hit_at);

CREATE TABLE providers (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  base_url     TEXT NOT NULL,
  api_key      TEXT NOT NULL DEFAULT '',
  model        TEXT NOT NULL,
  priority     INTEGER NOT NULL DEFAULT 1,
  rpm          INTEGER,
  max_parallel INTEGER,
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  updated_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE app_config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE stats_daily (
  day               TEXT PRIMARY KEY,
  cache_hits        INTEGER NOT NULL DEFAULT 0,
  cache_misses      INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_failures      INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE provider_stats_daily (
  day               TEXT NOT NULL,
  provider_id       TEXT NOT NULL,
  calls             INTEGER NOT NULL DEFAULT 0,
  failures          INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider_id)
);

CREATE TABLE call_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  kind              TEXT NOT NULL,
  cache_key         TEXT,
  question          TEXT,
  provider_id       TEXT,
  model             TEXT,
  latency_ms        INTEGER,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  error             TEXT
);
CREATE INDEX idx_call_log_ts ON call_log(ts);
"""

SCHEMA_V2 = "DROP INDEX IF EXISTS idx_questions_created;"

# 版本号 → 该版本要执行的 DDL；新增版本只追加条目
MIGRATIONS: dict[int, str] = {1: SCHEMA_V1, 2: SCHEMA_V2}


class Database:
    """aiosqlite 连接的薄封装：autocommit 模式，显式事务走 transaction()。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        await self._migrate(conn)
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cursor = await self._run(sql, params)
        return cursor.rowcount

    async def insert(self, sql: str, params: tuple = ()) -> int | None:
        cursor = await self._run(sql, params)
        return cursor.lastrowid

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        async with await self._run(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        async with await self._run(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        conn = self._require_conn()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            await conn.execute("COMMIT")
        except BaseException:
            await conn.execute("ROLLBACK")
            raise

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        current = int(row[0])
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            await conn.executescript(MIGRATIONS[version])
            await conn.execute(f"PRAGMA user_version = {version}")
            logger.info("数据库迁移至 v%d: %s", version, self._path)

    async def _run(self, sql: str, params: tuple) -> Any:
        conn = self._require_conn()
        last_error: Exception | None = None
        for attempt in range(BUSY_RETRIES + 1):
            try:
                return await conn.execute(sql, params)
            except sqlite3.OperationalError as error:
                last_error = error
                if "locked" not in str(error) or attempt == BUSY_RETRIES:
                    raise
                await asyncio.sleep(BUSY_RETRY_DELAY_SECONDS)
        raise last_error  # pragma: no cover - 循环内必已 return 或 raise

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未连接：请先调用 connect()")
        return self._conn
