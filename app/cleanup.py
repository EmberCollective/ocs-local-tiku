"""TTL 批次清理后台任务（design §9）。

每轮热读设置（ttl_days / cleanup_batch_size / cleanup_interval_hours，改设置无需重启），
分批删除超期未命中的缓存题目，并裁剪 7 天前的调用日志；单轮失败只记日志，
下一轮继续——清理异常绝不拖垮查询服务。
"""

import asyncio
import logging
import time

from app.db import Database
from app.repository.settings import DEFAULTS, SettingsRepo

logger = logging.getLogger(__name__)

INITIAL_DELAY_SECONDS = 60  # 启动后 1 分钟首轮
BATCH_PAUSE_SECONDS = 2     # 批间让出，避免持续锁库
LOG_RETENTION_SECONDS = 7 * 86400
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# DELETE ... LIMIT 依赖编译选项（Python 自带 SQLite 未启用），统一用 IN 子查询写法
DELETE_EXPIRED_BATCH_SQL = """
DELETE FROM questions WHERE cache_key IN (
    SELECT cache_key FROM questions
    WHERE last_hit_at < ? ORDER BY last_hit_at LIMIT ?
)
"""
TRIM_CALL_LOG_SQL = "DELETE FROM call_log WHERE ts < ?"


async def cleanup_loop(db: Database, settings_repo: SettingsRepo) -> None:
    """清理主循环：首轮延迟启动，此后「清理一轮 → 休眠间隔小时」往复。"""
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while True:
        interval_hours = await _run_round(db, settings_repo)
        await asyncio.sleep(interval_hours * SECONDS_PER_HOUR)


async def _run_round(db: Database, settings_repo: SettingsRepo) -> int:
    """单轮清理（热读设置 → 分批删超期 → 裁日志），返回休眠小时数；失败回退默认间隔。"""
    try:
        ttl_days = int(await settings_repo.get("ttl_days"))
        batch_size = int(await settings_repo.get("cleanup_batch_size"))
        await _purge_expired(db, ttl_days, batch_size)
        await _trim_call_log(db)
        return int(await settings_repo.get("cleanup_interval_hours"))
    except Exception:
        logger.exception("TTL 清理轮次失败，等待下一轮")
        return DEFAULTS["cleanup_interval_hours"]


async def _purge_expired(db: Database, ttl_days: int, batch_size: int) -> None:
    """按 last_hit_at 分批删除 TTL 外的行，删到不足一批为止。"""
    cutoff = int(time.time()) - ttl_days * SECONDS_PER_DAY  # epoch 差值，时区无关
    while True:
        deleted = await db.execute(DELETE_EXPIRED_BATCH_SQL, (cutoff, batch_size))
        if deleted < batch_size:
            break
        await asyncio.sleep(BATCH_PAUSE_SECONDS)


async def _trim_call_log(db: Database) -> None:
    """调用日志只保留近 7 天。"""
    await db.execute(TRIM_CALL_LOG_SQL, (int(time.time()) - LOG_RETENTION_SECONDS,))
