"""共享 fixtures：隔离数据目录、数据库、ASGI 客户端、FakeLLM。"""

import httpx
import pytest

from app.db import Database
from app.main import create_app

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
    "idx_questions_created",
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
async def client(data_dir):
    """httpx ASGI 客户端，自动进入/退出 lifespan（建库连接）。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
