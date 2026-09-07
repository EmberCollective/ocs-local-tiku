"""/api/query 端点：OCS 协议契约（恒 HTTP 200，业务失败 code=0）。"""

import pytest

pytestmark = pytest.mark.asyncio

TITLE = "下列哪项是中国的首都"
OPTIONS = "A. 北京\nB. 上海"


async def test_get_query_returns_code1(client):
    resp = await client.get(
        "/api/query", params={"title": TITLE, "type": "single", "options": OPTIONS}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 1
    assert body["question"] == TITLE
    assert body["answer"] == "A"  # EchoLLM 回第一选项 → 映射字母 A


async def test_post_json_body(client):
    resp = await client.post(
        "/api/query", json={"title": TITLE, "type": "single", "options": OPTIONS}
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == 1


async def test_second_query_hits_cache(client):
    first = (await client.get("/api/query", params={"title": TITLE, "options": OPTIONS})).json()
    second = (await client.get("/api/query", params={"title": TITLE, "options": OPTIONS})).json()
    assert first["code"] == second["code"] == 1
    assert second["answer"] == first["answer"]


async def test_missing_title_returns_code0(client):
    resp = await client.get("/api/query", params={"options": OPTIONS})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert "题目" in body["msg"]

    resp = await client.post("/api/query", json={"title": "  ", "options": OPTIONS})
    assert resp.json()["code"] == 0


async def test_invalid_type_falls_back_to_single(client):
    resp = await client.get(
        "/api/query", params={"title": TITLE, "type": "essay", "options": OPTIONS}
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == 1


async def test_without_provider_returns_code0(plain_client):
    resp = await plain_client.get("/api/query", params={"title": TITLE, "options": OPTIONS})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert "未配置" in body["msg"]


async def test_unexpected_exception_returns_code0(client):
    class BoomService:
        async def query(self, *args, **kwargs):
            raise RuntimeError("unexpected")

    client.app.state.answer_service = BoomService()
    resp = await client.get("/api/query", params={"title": TITLE})
    client.app.state.answer_service = None  # 恢复，避免影响 lifespan 清理
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert "msg" in body
