"""应用工厂：lifespan + CORS + 路由挂载。"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.answer.service import AnswerService
from app.api import (
    admin_cache,
    admin_providers,
    admin_settings,
    health,
    import_api,
    query,
)
from app.api.deps import require_token
from app.cleanup import cleanup_loop
from app.db import Database
from app.llm.fake import EchoLLM, UnconfiguredLLM
from app.llm.types import LLMProtocol
from app.repository import providers as providers_repo
from app.repository.settings import SettingsRepo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(config.db_path())
        await db.connect()
        app.state.db = db
        router_manager = await _build_router_manager(db)
        app.state.router_manager = router_manager
        app.state.answer_service = AnswerService(db, _build_llm(router_manager))
        cleanup_task = asyncio.create_task(cleanup_loop(db, SettingsRepo(db)))
        yield
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass  # 关闭路径的预期取消
        await db.close()

    app = FastAPI(title="ocs-local-tiku", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(query.router)
    # 管理路由统一挂可选鉴权；数据面（query/health/import）豁免
    app.include_router(admin_providers.router, dependencies=[Depends(require_token)])
    app.include_router(admin_cache.router, dependencies=[Depends(require_token)])
    app.include_router(admin_settings.router, dependencies=[Depends(require_token)])
    app.include_router(import_api.router)
    return app


async def _build_router_manager(db: Database):
    """非 FAKE 模式创建 RouterManager 并完成首轮构建；FAKE 模式返回 None。"""
    if config.fake_llm_enabled():
        return None
    from app.llm.router_manager import RouterManager  # 延迟导入：FAKE/单元测试不加载 litellm

    manager = RouterManager(
        db,
        list_enabled=lambda: providers_repo.list_enabled(db),
        settings=SettingsRepo(db),
    )
    await manager.rebuild()
    return manager


def _build_llm(router_manager) -> LLMProtocol:
    """FAKE 模式用 EchoLLM；生产路径用 RouterManager（无 provider 时 ask 报未配置）。"""
    if config.fake_llm_enabled():
        return EchoLLM()
    if router_manager is None:
        return UnconfiguredLLM()
    return router_manager


app = create_app()
