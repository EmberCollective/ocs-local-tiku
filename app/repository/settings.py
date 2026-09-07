"""app_config KV 仓储：读时与代码内 DEFAULTS 合并，新增键免迁移。"""

import json

from app.db import Database

DEFAULTS = {
    "ttl_days": 60,
    "cleanup_batch_size": 500,
    "cleanup_interval_hours": 6,
    "llm_timeout": 20,
    "num_retries": 1,
    "allowed_fails": 3,
    "cooldown_time": 60,
    "routing_strategy": "simple-shuffle",
    "api_token": "",
}

# 涉及 Router 构建的键：保存后需热重建
ROUTER_KEYS = frozenset(
    {"routing_strategy", "llm_timeout", "num_retries", "allowed_fails", "cooldown_time"}
)

_UPSERT_SQL = (
    "INSERT INTO app_config (key, value) VALUES (?, ?)"
    " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str):
        if key not in DEFAULTS:
            return None
        row = await self._db.query_one("SELECT value FROM app_config WHERE key = ?", (key,))
        return json.loads(row["value"]) if row is not None else DEFAULTS[key]

    async def get_all(self) -> dict:
        rows = await self._db.query("SELECT key, value FROM app_config")
        stored = {row["key"]: json.loads(row["value"]) for row in rows if row["key"] in DEFAULTS}
        return {**DEFAULTS, **stored}

    async def put(self, settings: dict) -> None:
        for key, value in settings.items():
            if key not in DEFAULTS:
                continue
            await self._db.execute(_UPSERT_SQL, (key, json.dumps(value)))
