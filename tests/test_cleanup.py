"""TTL 批次清理：分批删除超期缓存、裁剪调用日志、设置每轮热读、失败不退出。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import cleanup
from app.repository.settings import SettingsRepo

pytestmark = pytest.mark.asyncio

ROUND_TIMEOUT_SECONDS = 5.0


class SleepStub:
    """替身 asyncio.sleep：短睡放行；长睡（轮间隔）计轮数并挂起等待测试放行。"""

    def __init__(self, interval_threshold: float = 3600.0) -> None:
        self.interval_threshold = interval_threshold
        self.sleeps: list[float] = []
        self.rounds_finished = 0
        self._progress = asyncio.Event()
        self._release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds < self.interval_threshold:
            return
        self.rounds_finished += 1
        self._progress.set()
        await self._release.wait()
        self._release.clear()

    async def wait_for_round(self, count: int) -> None:
        async def wait_until_count() -> None:
            while self.rounds_finished < count:
                await self._progress.wait()
                self._progress.clear()

        await asyncio.wait_for(wait_until_count(), ROUND_TIMEOUT_SECONDS)

    def release(self) -> None:
        self._release.set()


class FlakySettings(SettingsRepo):
    """前 failures 次 get 抛错，模拟设置读取故障。"""

    def __init__(self, db, failures: int) -> None:
        super().__init__(db)
        self._failures = failures

    async def get(self, key: str):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("模拟设置读取故障")
        return await super().get(key)


async def _insert_question(db, cache_key: str, last_hit_at: int) -> None:
    await db.insert(
        "INSERT INTO questions (cache_key, question, qtype, options_json, answer, source, last_hit_at)"
        " VALUES (?, ?, 'single', '[]', ?, 'import', ?)",
        (cache_key, cache_key, cache_key, last_hit_at),
    )


def _days_ago(days: int) -> int:
    return int(time.time()) - days * 86400


def _install_sleep_stub(monkeypatch) -> SleepStub:
    stub = SleepStub()
    monkeypatch.setattr(cleanup, "asyncio", SimpleNamespace(sleep=stub.sleep))
    return stub


def _spy_question_deletes(db, monkeypatch) -> list[tuple]:
    """记录 DELETE FROM questions 的批参数，用于断言分批行为。"""
    calls: list[tuple] = []
    original_execute = db.execute

    async def counting_execute(sql: str, params: tuple = ()):
        if sql.strip().startswith("DELETE FROM questions"):
            calls.append(params)
        return await original_execute(sql, params)

    monkeypatch.setattr(db, "execute", counting_execute)
    return calls


async def _count(db, table: str) -> int:
    row = await db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return row["n"] if row else 0


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_purges_expired_in_batches_and_trims_log(db, monkeypatch):
    """501 过期 + 5 新鲜、batch=500 → 恰删 501（两批）、新鲜保留；超 7 天日志裁剪。"""
    settings = SettingsRepo(db)
    await settings.put({"ttl_days": 60, "cleanup_batch_size": 500})
    for i in range(501):
        await _insert_question(db, f"old-{i}", _days_ago(365))
    for i in range(5):
        await _insert_question(db, f"new-{i}", _days_ago(1))
    await db.insert("INSERT INTO call_log (kind, ts) VALUES ('hit', ?)", (_days_ago(8),))
    await db.insert("INSERT INTO call_log (kind, ts) VALUES ('hit', ?)", (_days_ago(1),))

    stub = _install_sleep_stub(monkeypatch)
    delete_calls = _spy_question_deletes(db, monkeypatch)
    task = asyncio.create_task(cleanup.cleanup_loop(db, settings))
    try:
        await stub.wait_for_round(1)
    finally:
        await _cancel(task)

    assert await _count(db, "questions") == 5
    remaining = {row["cache_key"] for row in await db.query("SELECT cache_key FROM questions")}
    assert remaining == {f"new-{i}" for i in range(5)}
    assert len(delete_calls) == 2  # 500 + 1，两批删完
    assert all(params[1] == 500 for params in delete_calls)
    assert await _count(db, "call_log") == 1  # 8 天前的裁掉，1 天前的保留


async def test_ttl_zero_deletes_nothing_fresh(db, monkeypatch):
    """TTL 内的行不受影响：首轮后 30 天前的行在 ttl=60 下保留。"""
    settings = SettingsRepo(db)
    await settings.put({"ttl_days": 60})
    await _insert_question(db, "recent", _days_ago(30))

    stub = _install_sleep_stub(monkeypatch)
    task = asyncio.create_task(cleanup.cleanup_loop(db, settings))
    try:
        await stub.wait_for_round(1)
    finally:
        await _cancel(task)

    assert await _count(db, "questions") == 1


async def test_settings_hot_reload_between_rounds(db, monkeypatch):
    """每轮热读设置：ttl 与 interval 改动后下一轮立即生效。"""
    settings = SettingsRepo(db)
    await settings.put({"ttl_days": 60, "cleanup_interval_hours": 6})
    await _insert_question(db, "mid", _days_ago(30))
    await _insert_question(db, "ancient", _days_ago(90))

    stub = _install_sleep_stub(monkeypatch)
    task = asyncio.create_task(cleanup.cleanup_loop(db, settings))
    try:
        await stub.wait_for_round(1)
        assert await _count(db, "questions") == 1  # 仅 90 天前的被删
        assert 6 * 3600 in stub.sleeps

        await settings.put({"ttl_days": 7, "cleanup_interval_hours": 1})
        stub.release()
        await stub.wait_for_round(2)
        assert await _count(db, "questions") == 0  # ttl=7 下 30 天前的也被删
        assert 1 * 3600 in stub.sleeps
    finally:
        await _cancel(task)


async def test_batch_size_hot_reload_splits_round(db, monkeypatch):
    """cleanup_batch_size 热读：batch=2 时 5 行分三批删。"""
    settings = SettingsRepo(db)
    await settings.put({"ttl_days": 1, "cleanup_batch_size": 500})
    for i in range(5):
        await _insert_question(db, f"expired-{i}", _days_ago(30))

    stub = _install_sleep_stub(monkeypatch)
    delete_calls = _spy_question_deletes(db, monkeypatch)
    task = asyncio.create_task(cleanup.cleanup_loop(db, settings))
    try:
        await stub.wait_for_round(1)
        assert await _count(db, "questions") == 0
        assert [params[1] for params in delete_calls] == [500]
        assert stub.sleeps.count(cleanup.BATCH_PAUSE_SECONDS) == 0  # 5 < 500，无批间让出

        await settings.put({"cleanup_batch_size": 2})
        for i in range(5):
            await _insert_question(db, f"again-{i}", _days_ago(30))
        stub.release()
        await stub.wait_for_round(2)
        assert await _count(db, "questions") == 0
        assert [params[1] for params in delete_calls[1:]] == [2, 2, 2]  # 2+2+1 三批
        # 两个满批之后各让出一次
        assert stub.sleeps.count(cleanup.BATCH_PAUSE_SECONDS) == 2
    finally:
        await _cancel(task)


async def test_round_failure_keeps_loop_alive(db, monkeypatch, caplog):
    """单轮失败只记日志，下一轮继续清理。"""
    flaky = FlakySettings(db, failures=1)
    await SettingsRepo(db).put({"ttl_days": 60})
    await _insert_question(db, "expired", _days_ago(365))

    stub = _install_sleep_stub(monkeypatch)
    with caplog.at_level("ERROR", logger="app.cleanup"):
        task = asyncio.create_task(cleanup.cleanup_loop(db, flaky))
        try:
            await stub.wait_for_round(1)  # 首轮失败：间隔休眠被挂起
            stub.release()
            await stub.wait_for_round(2)
            assert await _count(db, "questions") == 0  # 第二轮补救成功
            assert not task.done()  # 循环仍存活
        finally:
            await _cancel(task)
    assert "清理轮次失败" in caplog.text
