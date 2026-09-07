"""共享 fixtures：隔离数据目录、ASGI 客户端、FakeLLM。"""

import httpx
import pytest

from app.main import create_app


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """每个测试独立的 DATA_DIR，保证 SQLite 完全隔离。"""
    directory = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(directory))
    return directory


@pytest.fixture
async def client(data_dir):
    """httpx ASGI 客户端，自动进入/退出 lifespan（建库连接）。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
