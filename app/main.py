"""应用工厂：lifespan + CORS + 路由挂载。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.answer.service import AnswerService
from app.api import health, query
from app.db import Database
from app.llm.fake import EchoLLM, UnconfiguredLLM
from app.llm.types import LLMProtocol

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(config.db_path())
        await db.connect()
        app.state.db = db
        app.state.answer_service = AnswerService(db, _build_llm())
        yield
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
    return app


def _build_llm() -> LLMProtocol:
    """M1：冒烟开关决定 EchoLLM/占位；M2 起替换为 RouterManager。"""
    if config.fake_llm_enabled():
        return EchoLLM()
    return UnconfiguredLLM()


app = create_app()
