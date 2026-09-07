"""管理 API：providers CRUD、reorder、直连测试、近 24h 状态（design §7）。

api_key 仅内部使用，序列化一律打码（mask_key）；写操作成功后热重建 Router。
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_db, rebuild_router
from app.db import Database
from app.models import Provider
from app.repository import providers as providers_repo
from app.repository import stats as stats_repo
from app.repository.providers import mask_key

router = APIRouter()

# 近 24h 状态窗口（day 粒度取昨日+今日；call_log 按 epoch 秒）
STATUS_WINDOW_SECONDS = 86400


class ProviderCreate(BaseModel):
    """新增 provider 请求体。"""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = ""
    model: str = Field(min_length=1)
    rpm: int | None = None
    max_parallel: int | None = None


class ProviderUpdate(BaseModel):
    """部分更新请求体：缺省字段不改；api_key 留空 = 不修改。"""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, min_length=1)
    api_key: str | None = None
    model: str | None = Field(default=None, min_length=1)
    rpm: int | None = None
    max_parallel: int | None = None


class ReorderBody(BaseModel):
    """按序重排请求体：列表顺序 = 优先级。"""

    ids: list[str]


class EnabledBody(BaseModel):
    """启停请求体。"""

    enabled: bool


@router.get("/api/providers")
async def list_providers(db: Database = Depends(get_db)) -> dict:
    return {"items": [_serialize(p) for p in await providers_repo.list_providers(db)]}


@router.post("/api/providers")
async def create_provider(
    body: ProviderCreate, request: Request, db: Database = Depends(get_db)
) -> dict:
    provider = await providers_repo.create_provider(db, body.model_dump())
    await rebuild_router(request)
    return _serialize(provider)


@router.get("/api/providers/status")
async def providers_status(request: Request, db: Database = Depends(get_db)) -> dict:
    """近 24h 各 provider 调用/失败/token、成功率、平均延迟、冷却标记。"""
    all_providers = await providers_repo.list_providers(db)
    counters = await stats_repo.provider_stats_since(db, stats_repo.days_ago(1))
    latencies = await stats_repo.avg_latency_since(
        db, int(time.time()) - STATUS_WINDOW_SECONDS
    )
    cooldown_entries = _cooldown_entries(request)
    items = [
        _status_dict(p, counters.get(p.id), latencies.get(p.id), cooldown_entries)
        for p in all_providers
    ]
    return {"items": items}


@router.post("/api/providers/reorder")
async def reorder_providers(
    body: ReorderBody, request: Request, db: Database = Depends(get_db)
) -> dict:
    try:
        await providers_repo.reorder(db, body.ids)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await rebuild_router(request)
    listed = await providers_repo.list_providers(db)
    return {"items": [_serialize(p) for p in listed]}


@router.put("/api/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    if await providers_repo.get_provider(db, provider_id) is None:
        raise HTTPException(status_code=404, detail="provider 不存在")
    await providers_repo.update_provider(db, provider_id, body.model_dump(exclude_unset=True))
    await rebuild_router(request)
    updated = await providers_repo.get_provider(db, provider_id)
    return _serialize(updated)


@router.put("/api/providers/{provider_id}/enabled")
async def set_provider_enabled(
    provider_id: str,
    body: EnabledBody,
    request: Request,
    db: Database = Depends(get_db),
) -> dict:
    """启停专用端点（enabled 不在通用更新字段内）。"""
    if await providers_repo.get_provider(db, provider_id) is None:
        raise HTTPException(status_code=404, detail="provider 不存在")
    await providers_repo.set_enabled(db, provider_id, body.enabled)
    await rebuild_router(request)
    updated = await providers_repo.get_provider(db, provider_id)
    return _serialize(updated)


@router.delete("/api/providers/{provider_id}")
async def delete_provider(
    provider_id: str, request: Request, db: Database = Depends(get_db)
) -> dict:
    if not await providers_repo.delete_provider(db, provider_id):
        raise HTTPException(status_code=404, detail="provider 不存在")
    await rebuild_router(request)
    return {"deleted": provider_id}


@router.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request, db: Database = Depends(get_db)) -> dict:
    """直连单 provider 冒烟（不走 Router，10s 超时）。"""
    provider = await providers_repo.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider 不存在")
    manager = getattr(request.app.state, "router_manager", None)
    if manager is None:
        return {"ok": False, "latency_ms": 0, "reply": None, "error": "Router 未初始化"}
    return await manager.test_provider(provider)


def _serialize(provider: Provider) -> dict:
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "api_key": mask_key(provider.api_key),
        "model": provider.model,
        "priority": provider.priority,
        "rpm": provider.rpm,
        "max_parallel": provider.max_parallel,
        "enabled": provider.enabled,
        "created_at": provider.created_at,
        "updated_at": provider.updated_at,
    }


def _status_dict(
    provider: Provider, counters: dict | None, avg_latency: float | None, cooldowns: list[str]
) -> dict:
    calls = counters["calls"] if counters else 0
    failures = counters["failures"] if counters else 0
    attempts = calls + failures
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.model,
        "enabled": provider.enabled,
        "calls": calls,
        "failures": failures,
        "prompt_tokens": counters["prompt_tokens"] if counters else 0,
        "completion_tokens": counters["completion_tokens"] if counters else 0,
        "success_rate": (calls / attempts) if attempts else None,
        "avg_latency_ms": avg_latency,
        "cooldown": _is_cooldown(provider, cooldowns),
    }


def _cooldown_entries(request: Request) -> list[str]:
    """Router 冷却标记（best-effort：Manager 缺失或异常均返回空）。"""
    manager = getattr(request.app.state, "router_manager", None)
    if manager is None:
        return []
    try:
        return [str(entry) for entry in manager.cooldowns()]
    except Exception:
        return []


def _is_cooldown(provider: Provider, cooldowns: list[str]) -> bool:
    """litellm 冷却条目格式不保证，按 id/base_url/model 子串匹配（best-effort）。"""
    return any(
        provider.id in entry or provider.base_url in entry or provider.model in entry
        for entry in cooldowns
    )
