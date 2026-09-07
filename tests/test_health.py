"""/api/health 免鉴权探活端点。"""


async def test_health_returns_ok(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
