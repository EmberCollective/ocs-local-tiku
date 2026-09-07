"""数据库层：版本化迁移、幂等、SQLITE_BUSY 重试、基础读写。"""

import sqlite3

from tests.conftest import EXPECTED_INDEXES, EXPECTED_TABLES


async def test_migrate_creates_all_tables_and_indexes(db):
    rows = await db.query("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= names
    assert EXPECTED_INDEXES <= names


async def test_migrate_sets_user_version(db):
    row = await db.query_one("PRAGMA user_version")
    assert row["user_version"] == 1


async def test_connect_is_idempotent_same_instance(db):
    await db.connect()  # 已连接时直接返回，不重复迁移
    row = await db.query_one("PRAGMA user_version")
    assert row["user_version"] == 1


async def test_reopen_existing_db_skips_migration(data_dir):
    from app.db import Database

    first = Database(data_dir / "tiku.db")
    await first.connect()
    await first.close()

    second = Database(data_dir / "tiku.db")
    await second.connect()  # user_version 守护：不重复建表
    rows = await second.query("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = {row["name"] for row in rows}
    await second.close()
    assert table_names == EXPECTED_TABLES | {"sqlite_sequence"}


async def test_execute_insert_query_roundtrip(db):
    n = await db.execute(
        "INSERT INTO app_config(key, value) VALUES(?, ?)", ("greeting", '"hello"')
    )
    assert n == 1
    row = await db.query_one("SELECT value FROM app_config WHERE key = ?", ("greeting",))
    assert row["value"] == '"hello"'


async def test_insert_returns_lastrowid(db):
    row_id = await db.insert(
        "INSERT INTO call_log(kind, question) VALUES(?, ?)", ("miss", "题目一")
    )
    assert row_id == 1


async def test_transaction_rollback_on_error(db):
    class Boom(Exception):
        pass

    try:
        async with db.transaction():
            await db.execute("INSERT INTO app_config(key, value) VALUES(?, ?)", ("k", "1"))
            raise Boom
    except Boom:
        pass
    assert await db.query_one("SELECT * FROM app_config WHERE key = 'k'") is None


async def test_execute_retries_once_on_locked(db, monkeypatch):
    real_conn = db._conn

    class FlakyConn:
        """第一次 INSERT 抛 database is locked，之后放行。"""

        def __init__(self, inner):
            self._inner = inner
            self.has_failed = False

        async def execute(self, sql, params=()):
            if not self.has_failed and sql.upper().startswith("INSERT"):
                self.has_failed = True
                raise sqlite3.OperationalError("database is locked")
            return await self._inner.execute(sql, params)

    monkeypatch.setattr(db, "_conn", FlakyConn(real_conn))
    await db.execute("INSERT INTO app_config(key, value) VALUES(?, ?)", ("k", "1"))
    monkeypatch.setattr(db, "_conn", real_conn)
    row = await db.query_one("SELECT value FROM app_config WHERE key = 'k'")
    assert row["value"] == "1"
