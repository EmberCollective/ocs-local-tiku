"""providers 仓储：CRUD、按序重排 priority、启停、api_key 打码。"""

import time
import uuid

from app.db import Database
from app.models import Provider

# update_provider 允许修改的字段（priority 只能通过 reorder 变更；enabled 走通用更新）
UPDATABLE_FIELDS = ("name", "base_url", "api_key", "model", "rpm", "max_parallel", "enabled")


async def create_provider(db: Database, payload: dict) -> Provider:
    """新增 provider：uuid 主键，priority 追加到末位。"""
    row = await db.query_one("SELECT COALESCE(MAX(priority), 0) AS max_priority FROM providers")
    provider = Provider(
        id=uuid.uuid4().hex,
        name=payload["name"],
        base_url=payload["base_url"],
        api_key=payload.get("api_key", ""),
        model=payload["model"],
        priority=(row["max_priority"] if row else 0) + 1,
        rpm=_optional_int(payload.get("rpm")),
        max_parallel=_optional_int(payload.get("max_parallel")),
    )
    await db.insert(
        "INSERT INTO providers (id, name, base_url, api_key, model, priority, rpm, max_parallel)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _provider_params(provider),
    )
    return provider


async def get_provider(db: Database, provider_id: str) -> Provider | None:
    row = await db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
    return _to_provider(row) if row is not None else None


async def list_providers(db: Database) -> list[Provider]:
    rows = await db.query("SELECT * FROM providers ORDER BY priority, created_at")
    return [_to_provider(row) for row in rows]


async def list_enabled(db: Database) -> list[Provider]:
    rows = await db.query("SELECT * FROM providers WHERE enabled = 1 ORDER BY priority, created_at")
    return [_to_provider(row) for row in rows]


async def update_provider(db: Database, provider_id: str, fields: dict) -> None:
    """部分更新；api_key 留空 = 不修改（UI 密码框语义）。"""
    assignments, params = [], []
    for name in UPDATABLE_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if name == "api_key" and not value:
            continue
        if name in ("rpm", "max_parallel"):
            value = _optional_int(value)
        assignments.append(f"{name} = ?")
        params.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(provider_id)
    await db.execute(f"UPDATE providers SET {', '.join(assignments)} WHERE id = ?", tuple(params))


async def delete_provider(db: Database, provider_id: str) -> bool:
    return await db.execute("DELETE FROM providers WHERE id = ?", (provider_id,)) == 1


async def reorder(db: Database, ids: list[str]) -> None:
    """列表顺序 = 优先级：按传入顺序重写 priority（1 起）。"""
    existing = {p.id for p in await list_providers(db)}
    if set(ids) != existing or len(ids) != len(existing):
        raise ValueError("reorder 的 id 列表必须与现存 provider 完全一致")
    for position, provider_id in enumerate(ids, start=1):
        await db.execute(
            "UPDATE providers SET priority = ?, updated_at = ? WHERE id = ?",
            (position, int(time.time()), provider_id),
        )


async def set_enabled(db: Database, provider_id: str, enabled: bool) -> None:
    """专用启停写入；API 层已并入通用更新，保留供仓储层直用（tests 直接调用）。"""
    await db.execute(
        "UPDATE providers SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, int(time.time()), provider_id),
    )


def mask_key(key: str) -> str:
    """api_key 打码：保留短前缀（如 sk-）与末 3-4 字符。"""
    if not key:
        return "***"
    head, separator, _ = key.partition("-")
    prefix = f"{head}{separator}" if separator and len(head) <= 6 else ""
    tail = key[-4:] if len(key) > 8 else key[-3:]
    return f"{prefix}***{tail}"


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _provider_params(provider: Provider) -> tuple:
    return (
        provider.id,
        provider.name,
        provider.base_url,
        provider.api_key,
        provider.model,
        provider.priority,
        provider.rpm,
        provider.max_parallel,
    )


def _to_provider(row: dict) -> Provider:
    return Provider(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        api_key=row["api_key"],
        model=row["model"],
        priority=row["priority"],
        rpm=row["rpm"],
        max_parallel=row["max_parallel"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
