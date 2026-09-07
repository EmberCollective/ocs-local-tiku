"""API 层依赖注入：AppState 访问器、可选 token 校验、Router 热重建。"""

import secrets

from fastapi import HTTPException, Request

from app.answer.service import AnswerService
from app.db import Database
from app.repository.settings import SettingsRepo


def get_answer_service(request: Request) -> AnswerService:
    return request.app.state.answer_service


def get_db(request: Request) -> Database:
    return request.app.state.db


async def require_token(request: Request) -> None:
    """管理路由鉴权：api_token 非空时校验 X-Token 头或 ?token= 参数。

    token 为空时放行（本地零配置）；数据面（query/import/health）不挂本依赖。
    """
    token = await SettingsRepo(request.app.state.db).get("api_token")
    if not token:
        return
    provided = request.headers.get("X-Token") or request.query_params.get("token", "")
    if not secrets.compare_digest(provided.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def get_router_manager(request: Request):
    """app.state.router_manager 访问器（FAKE 模式下为 None）。

    返回类型不标注：RouterManager 延迟导入（避免 FAKE/单测加载 litellm）。
    """
    return getattr(request.app.state, "router_manager", None)


async def rebuild_router(request: Request) -> None:
    """providers/settings 写操作成功后热重建 Router。

    FAKE_LLM 模式下不创建 RouterManager，此处跳过。
    """
    manager = get_router_manager(request)
    if manager is not None:
        await manager.rebuild()
