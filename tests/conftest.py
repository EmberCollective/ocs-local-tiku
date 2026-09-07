"""共享 fixtures：隔离数据目录、数据库、ASGI 客户端、FakeLLM。"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app.db import Database
from app.llm.types import AskResult
from app.main import create_app


class FakeLLM:
    """测试替身：预设 reply/delay/error，计数调用。"""

    def __init__(self, reply: str = "A. 甲", delay: float = 0.0, error: Exception | None = None):
        self.reply = reply
        self.delay = delay
        self.error = error
        self.calls = 0
        self.last_messages: list[dict] | None = None

    async def ask(self, messages: list[dict]) -> AskResult:
        self.calls += 1
        self.last_messages = messages
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return AskResult(
            content=self.reply,
            model="fake-model",
            provider_id="fake",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1,
        )

EXPECTED_TABLES = {
    "questions",
    "providers",
    "app_config",
    "stats_daily",
    "provider_stats_daily",
    "call_log",
}
EXPECTED_INDEXES = {
    "idx_questions_last_hit",
    "idx_call_log_ts",
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """每个测试独立的 DATA_DIR，保证 SQLite 完全隔离。"""
    directory = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(directory))
    return directory


@pytest.fixture
async def db(data_dir):
    """已迁移的数据库连接，测试结束自动关闭。"""
    database = Database(data_dir / "tiku.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def client(data_dir, monkeypatch):
    """EchoLLM 冒烟客户端（TIKU_FAKE_LLM=1），client.app 暴露应用实例。"""
    monkeypatch.setenv("TIKU_FAKE_LLM", "1")
    async with _open_client(create_app()) as http_client:
        yield http_client


@pytest.fixture
async def plain_client(data_dir):
    """未启用 FakeLLM 的客户端（真实 RouterManager 路径，无 provider 时 ask 报未配置）。"""
    async with _open_client(create_app()) as http_client:
        yield http_client


@asynccontextmanager
async def _open_client(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            http_client.app = app  # 测试可触达 app.state
            yield http_client
