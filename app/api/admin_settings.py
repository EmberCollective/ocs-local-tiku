"""管理 API：设置读写（Router 键保存后热重建）与调用日志（design §7）。"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_db, rebuild_router
from app.db import Database
from app.repository import stats as stats_repo
from app.repository.settings import ROUTER_KEYS, SettingsRepo

router = APIRouter()

DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 500


class SettingsUpdate(BaseModel):
    """设置部分更新：缺省键不改，未知键忽略。"""

    model_config = ConfigDict(extra="ignore")

    ttl_days: int | None = Field(default=None, ge=1)
    cleanup_batch_size: int | None = Field(default=None, ge=1)
    cleanup_interval_hours: int | None = Field(default=None, ge=1)
    llm_timeout: int | None = Field(default=None, ge=1)
    num_retries: int | None = Field(default=None, ge=0)
    allowed_fails: int | None = Field(default=None, ge=1)
    cooldown_time: int | None = Field(default=None, ge=1)
    routing_strategy: str | None = Field(default=None, min_length=1)
    api_token: str | None = None


@router.get("/api/settings")
async def get_settings(db: Database = Depends(get_db)) -> dict:
    return await SettingsRepo(db).get_all()


@router.put("/api/settings")
async def put_settings(
    body: SettingsUpdate, request: Request, db: Database = Depends(get_db)
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    repo = SettingsRepo(db)
    await repo.put(fields)
    if set(fields) & ROUTER_KEYS:
        await rebuild_router(request)
    return await repo.get_all()


@router.get("/api/log")
async def get_log(
    limit: int = Query(default=DEFAULT_LOG_LIMIT, ge=1, le=MAX_LOG_LIMIT),
    kind: str | None = None,
    db: Database = Depends(get_db),
) -> dict:
    return {"items": await stats_repo.list_log(db, limit=limit, kind=kind)}
