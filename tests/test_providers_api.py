"""providers 管理 API：CRUD、打码返回、reorder、直连测试、状态聚合、可选鉴权。"""

import pytest

from app.repository import stats as stats_repo
from app.repository.settings import SettingsRepo

pytestmark = pytest.mark.asyncio

PAYLOAD = {
    "name": "DeepSeek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-abc123def456",
    "model": "deepseek-chat",
}


class RouterSpy:
    """记录 rebuild/cooldowns/test_provider 调用的假 RouterManager。"""

    def __init__(self, cooldown_entries=None):
        self.rebuilds = 0
        self.tested_ids: list[str] = []
        self.cooldown_entries = cooldown_entries or []
        self.test_result = {"ok": True, "latency_ms": 3, "reply": "pong", "error": None}

    async def rebuild(self) -> None:
        self.rebuilds += 1

    def cooldowns(self) -> list[str]:
        return list(self.cooldown_entries)

    async def test_provider(self, provider) -> dict:
        self.tested_ids.append(provider.id)
        return dict(self.test_result)


@pytest.fixture
def spy(client):
    """把 app.state.router_manager 换成记录器（FAKE 模式下原本不存在）。"""
    recorder = RouterSpy()
    client.app.state.router_manager = recorder
    yield recorder
    client.app.state.router_manager = None


async def create_provider(client, **overrides) -> dict:
    resp = await client.post("/api/providers", json={**PAYLOAD, **overrides})
    assert resp.status_code == 200
    return resp.json()


class TestProvidersCrud:
    async def test_create_returns_masked_key(self, client):
        created = await create_provider(client)
        assert created["api_key"] == "sk-***f456"
        assert created["name"] == "DeepSeek"
        assert created["priority"] == 1
        assert created["enabled"] is True

    async def test_list_masks_keys_and_hides_raw(self, client):
        await create_provider(client)
        resp = await client.get("/api/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["api_key"] == "sk-***f456"
        assert "sk-abc123def456" not in resp.text

    async def test_create_requires_name(self, client):
        resp = await client.post("/api/providers", json={"base_url": "x", "model": "m"})
        assert resp.status_code == 422

    async def test_empty_list(self, client):
        resp = await client.get("/api/providers")
        assert resp.json() == {"items": []}

    async def test_update_blank_api_key_keeps_original(self, client):
        created = await create_provider(client)
        resp = await client.put(
            f"/api/providers/{created['id']}", json={"name": "改名", "api_key": ""}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "改名"
        assert resp.json()["api_key"] == "sk-***f456"  # 留空 = 不修改

    async def test_update_changes_api_key_when_provided(self, client):
        created = await create_provider(client)
        resp = await client.put(f"/api/providers/{created['id']}", json={"api_key": "sk-newkey123"})
        assert resp.json()["api_key"] == "sk-***y123"

    async def test_update_missing_returns_404(self, client):
        resp = await client.put("/api/providers/missing", json={"name": "x"})
        assert resp.status_code == 404

    async def test_delete_and_missing_404(self, client):
        created = await create_provider(client)
        assert (await client.delete(f"/api/providers/{created['id']}")).status_code == 200
        assert (await client.get("/api/providers")).json()["items"] == []
        assert (await client.delete(f"/api/providers/{created['id']}")).status_code == 404

    async def test_write_operations_trigger_rebuild(self, client, spy):
        created = await create_provider(client)
        assert spy.rebuilds == 1
        await client.put(f"/api/providers/{created['id']}", json={"name": "改名"})
        assert spy.rebuilds == 2
        await client.delete(f"/api/providers/{created['id']}")
        assert spy.rebuilds == 3


class TestProvidersReorder:
    async def test_reorder_rewrites_priority_and_rebuilds(self, client, spy):
        first = await create_provider(client)
        second = await create_provider(client, name="Kimi")
        rebuilds_before = spy.rebuilds
        resp = await client.post(
            "/api/providers/reorder", json={"ids": [second["id"], first["id"]]}
        )
        assert resp.status_code == 200
        assert spy.rebuilds == rebuilds_before + 1
        items = (await client.get("/api/providers")).json()["items"]
        assert [item["name"] for item in items] == ["Kimi", "DeepSeek"]
        assert [item["priority"] for item in items] == [1, 2]

    async def test_reorder_with_unknown_id_returns_400(self, client):
        created = await create_provider(client)
        resp = await client.post(
            "/api/providers/reorder", json={"ids": [created["id"], "missing"]}
        )
        assert resp.status_code == 400


class TestProvidersTest:
    async def test_test_provider_calls_router_manager(self, client, spy):
        created = await create_provider(client)
        resp = await client.post(f"/api/providers/{created['id']}/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "latency_ms": 3, "reply": "pong", "error": None}
        assert spy.tested_ids == [created["id"]]

    async def test_test_provider_missing_returns_404(self, client):
        assert (await client.post("/api/providers/missing/test")).status_code == 404

    async def test_test_provider_without_router_manager(self, client):
        """FAKE 模式下 router_manager 不存在：返回 ok=False 而非 500。"""
        created = await create_provider(client)
        resp = await client.post(f"/api/providers/{created['id']}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]


class TestProvidersStatus:
    async def test_aggregates_last_24h(self, client, db):
        target = await create_provider(client)
        other = await create_provider(client, name="Kimi")
        stale_day = stats_repo.days_ago(3)

        await stats_repo.bump_provider_daily(
            db, stale_day, target["id"], calls=99, prompt_tokens=999
        )  # 窗口外，不计入
        await stats_repo.bump_provider_daily(
            db, stats_repo.today(), target["id"],
            calls=10, failures=2, prompt_tokens=100, completion_tokens=50,
        )
        for latency in (150, 250):
            await stats_repo.log_call(
                db, kind="miss", provider_id=target["id"], latency_ms=latency
            )

        resp = await client.get("/api/providers/status")
        assert resp.status_code == 200
        items = {item["id"]: item for item in resp.json()["items"]}
        assert items[target["id"]]["calls"] == 10
        assert items[target["id"]]["failures"] == 2
        assert items[target["id"]]["prompt_tokens"] == 100
        assert items[target["id"]]["avg_latency_ms"] == 200
        assert items[target["id"]]["success_rate"] == pytest.approx(10 / 12)
        assert items[target["id"]]["cooldown"] is False
        # 无统计的 provider 返回零值而非缺失
        assert items[other["id"]]["calls"] == 0
        assert items[other["id"]]["success_rate"] is None
        assert items[other["id"]]["avg_latency_ms"] is None

    async def test_marks_cooldown_providers(self, client):
        created = await create_provider(client)
        client.app.state.router_manager = RouterSpy(
            cooldown_entries=[f"answering ({PAYLOAD['base_url']})"]
        )
        try:
            items = (await client.get("/api/providers/status")).json()["items"]
        finally:
            client.app.state.router_manager = None
        assert items[0]["cooldown"] is True

    async def test_cooldowns_exception_is_best_effort(self, client):
        await create_provider(client)

        class ExplodingManager:
            def cooldowns(self):
                raise RuntimeError("cooldown api 变更")

        client.app.state.router_manager = ExplodingManager()
        try:
            resp = await client.get("/api/providers/status")
        finally:
            client.app.state.router_manager = None
        assert resp.status_code == 200
        assert resp.json()["items"][0]["cooldown"] is False


class TestAdminAuth:
    async def _set_token(self, client, token="secret"):
        await SettingsRepo(client.app.state.db).put({"api_token": token})

    async def test_admin_endpoints_reject_without_token(self, client):
        await self._set_token(client)
        resp = await client.get("/api/providers")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "unauthorized"}

    async def test_admin_accepts_header_or_query_token(self, client):
        await self._set_token(client)
        assert (await client.get("/api/providers", headers={"X-Token": "secret"})).status_code == 200
        assert (await client.get("/api/providers", params={"token": "secret"})).status_code == 200
        assert (await client.get("/api/providers", headers={"X-Token": "wrong"})).status_code == 401

    async def test_write_endpoints_also_require_token(self, client):
        await self._set_token(client)
        resp = await client.post("/api/providers", json=PAYLOAD)
        assert resp.status_code == 401

    async def test_empty_token_allows_all(self, client):
        resp = await client.get("/api/providers")
        assert resp.status_code == 200

    async def test_data_plane_exempt_from_token(self, client):
        await self._set_token(client)
        assert (await client.get("/api/health")).status_code == 200
        resp = await client.get("/api/query", params={"title": "题目", "options": "A. 甲"})
        assert resp.status_code == 200
        assert resp.json()["code"] == 1
