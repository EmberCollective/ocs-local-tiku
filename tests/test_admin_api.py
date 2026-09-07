"""管理 API：缓存分页/搜索/删除/导出、统计摘要、设置、调用日志。"""

import pytest

from app.models import NewQuestion
from app.repository import questions as questions_repo
from app.repository import stats as stats_repo
from app.repository.settings import DEFAULTS, SettingsRepo

pytestmark = pytest.mark.asyncio


async def seed_question(client, *, question, qtype="single", answer="甲") -> int:
    """直插缓存行，返回自增 id（不依赖 LLM 路径，完全可控）。"""
    record = NewQuestion(
        cache_key=f"key-{question}",
        question=question,
        qtype=qtype,
        options=["A. 甲", "B. 乙"],
        answer=answer,
        source="llm",
    )
    db = client.app.state.db
    await questions_repo.insert_question(db, record)
    row = await questions_repo.get_by_key(db, record.cache_key)
    return row.id


class TestCacheApi:
    async def test_list_with_pagination(self, client):
        for index in range(5):
            await seed_question(client, question=f"题目{index}")
        resp = await client.get("/api/cache")
        body = resp.json()
        assert resp.status_code == 200
        assert body["total"] == 5
        assert len(body["items"]) == 5
        assert body["page"] == 1 and body["page_size"] == 20
        assert body["items"][0]["question"] == "题目4"  # 新题在前

        page2 = (await client.get("/api/cache", params={"page": 2, "page_size": 2})).json()
        assert page2["total"] == 5
        assert [item["question"] for item in page2["items"]] == ["题目2", "题目1"]

    async def test_search_and_type_filter(self, client):
        await seed_question(client, question="光合作用发生在叶绿体")
        await seed_question(client, question="无关题目", qtype="judgement", answer="正确")
        by_keyword = (await client.get("/api/cache", params={"q": "光合作用"})).json()
        assert by_keyword["total"] == 1
        assert by_keyword["items"][0]["question"] == "光合作用发生在叶绿体"

        by_type = (await client.get("/api/cache", params={"type": "judgement"})).json()
        assert by_type["total"] == 1
        assert by_type["items"][0]["type"] == "judgement"

    async def test_invalid_pagination_422(self, client):
        assert (await client.get("/api/cache", params={"page": 0})).status_code == 422
        assert (await client.get("/api/cache", params={"page_size": 0})).status_code == 422
        assert (await client.get("/api/cache", params={"page_size": 201})).status_code == 422

    async def test_item_shape(self, client):
        await seed_question(client, question="题目")
        item = (await client.get("/api/cache")).json()["items"][0]
        assert set(item) == {
            "id", "cache_key", "question", "type", "options", "answer",
            "source", "created_at", "last_hit_at", "hits",
        }
        assert item["options"] == ["A. 甲", "B. 乙"]

    async def test_delete_single_and_404(self, client):
        question_id = await seed_question(client, question="题目")
        resp = await client.delete(f"/api/cache/{question_id}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 1}
        assert (await client.delete(f"/api/cache/{question_id}")).status_code == 404

    async def test_delete_batch(self, client):
        ids = [await seed_question(client, question=f"题目{index}") for index in range(3)]
        resp = await client.post("/api/cache/delete-batch", json={"ids": ids})
        assert resp.json() == {"deleted": 3}
        assert (await client.get("/api/cache")).json()["total"] == 0
        empty = await client.post("/api/cache/delete-batch", json={"ids": []})
        assert empty.json() == {"deleted": 0}

    async def test_delete_batch_over_limit_400(self, client):
        ids = list(range(1, 1002))
        resp = await client.post("/api/cache/delete-batch", json={"ids": ids})
        assert resp.status_code == 400
        assert "1000" in resp.json()["detail"]

    async def test_export_all(self, client):
        await seed_question(client, question="题目一", answer="甲")
        await seed_question(client, question="题目二", qtype="judgement", answer="正确")
        resp = await client.get("/api/cache/export")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        assert items[0] == {
            "question": "题目一", "type": "single", "options": ["A. 甲", "B. 乙"], "answer": "甲",
        }

    async def test_export_downloads_as_attachment(self, client):
        """导出带 attachment 头 → 浏览器落盘下载而不是新标签页展示。"""
        await seed_question(client, question="题目一")
        resp = await client.get("/api/cache/export")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="tiku-export-')
        assert disposition.endswith('.json"')
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["items"][0]["question"] == "题目一"


class TestStatsApi:
    async def test_shape_today_total_and_daily_series(self, client):
        await seed_question(client, question="题目一")
        await seed_question(client, question="题目二")
        await stats_repo.bump_daily(
            client.app.state.db, stats_repo.today(),
            cache_hits=3, cache_misses=1, llm_calls=1, prompt_tokens=10, completion_tokens=5,
        )
        await stats_repo.bump_daily(client.app.state.db, stats_repo.days_ago(10), cache_hits=7)
        await stats_repo.bump_daily(client.app.state.db, stats_repo.days_ago(20), cache_hits=100)

        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"today", "total", "daily"}

        assert body["today"]["cache_hits"] == 3
        assert body["today"]["day"] == stats_repo.today()
        assert body["total"]["cache_hits"] == 110  # 全部累计
        assert body["total"]["questions"] == 2

        daily = body["daily"]
        assert len(daily) == 14
        assert daily[-1]["day"] == stats_repo.today()
        by_day = {entry["day"]: entry for entry in daily}
        assert by_day[stats_repo.days_ago(10)]["cache_hits"] == 7
        assert stats_repo.days_ago(20) not in by_day  # 窗口外不进序列
        assert by_day[stats_repo.days_ago(1)]["cache_hits"] == 0  # 空档补零

    async def test_empty_stats_returns_zeros(self, client):
        body = (await client.get("/api/stats")).json()
        assert body["total"]["questions"] == 0
        assert body["today"]["llm_calls"] == 0
        assert len(body["daily"]) == 14


class RebuildRecorder:
    """记录 rebuild 调用次数的假 RouterManager。"""

    def __init__(self):
        self.rebuilds = 0

    async def rebuild(self) -> None:
        self.rebuilds += 1


class TestSettingsApi:
    async def test_get_returns_defaults_merged(self, client):
        await SettingsRepo(client.app.state.db).put({"ttl_days": 30})
        body = (await client.get("/api/settings")).json()
        assert body["ttl_days"] == 30
        assert body["cleanup_batch_size"] == DEFAULTS["cleanup_batch_size"]
        assert body["api_token"] == ""

    async def test_put_updates_and_returns_full_settings(self, client):
        resp = await client.put("/api/settings", json={"ttl_days": 30, "routing_strategy": "least-busy"})
        assert resp.status_code == 200
        assert resp.json()["ttl_days"] == 30
        assert (await client.get("/api/settings")).json()["routing_strategy"] == "least-busy"

    async def test_put_partial_keeps_other_keys(self, client):
        await client.put("/api/settings", json={"ttl_days": 30})
        await client.put("/api/settings", json={"cleanup_batch_size": 100})
        values = (await client.get("/api/settings")).json()
        assert values["ttl_days"] == 30
        assert values["cleanup_batch_size"] == 100

    async def test_put_unknown_keys_rejected_422(self, client):
        resp = await client.put("/api/settings", json={"not_a_setting": 1})
        assert resp.status_code == 422
        assert (await client.get("/api/settings")).json() == dict(DEFAULTS)

    async def test_put_invalid_value_422(self, client):
        assert (await client.put("/api/settings", json={"ttl_days": 0})).status_code == 422
        assert (await client.put("/api/settings", json={"api_token": 123})).status_code == 422

    async def test_api_token_roundtrip(self, client):
        await client.put("/api/settings", json={"api_token": "t0ken"})
        # 设置保存后立即生效：无 token 的后续请求被拒
        assert (await client.get("/api/settings")).status_code == 401
        values = (await client.get("/api/settings", headers={"X-Token": "t0ken"})).json()
        assert values["api_token"] == "t0ken"
        # 重置为空恢复零配置
        reset = await client.put(
            "/api/settings", json={"api_token": ""}, headers={"X-Token": "t0ken"}
        )
        assert reset.status_code == 200
        assert (await client.get("/api/settings")).json()["api_token"] == ""

    async def test_rebuild_triggered_only_for_router_keys(self, client):
        recorder = RebuildRecorder()
        client.app.state.router_manager = recorder
        try:
            await client.put("/api/settings", json={"ttl_days": 7})
            assert recorder.rebuilds == 0
            await client.put("/api/settings", json={"llm_timeout": 30})
            assert recorder.rebuilds == 1
            await client.put("/api/settings", json={"routing_strategy": "least-busy"})
            assert recorder.rebuilds == 2
        finally:
            client.app.state.router_manager = None


class TestLogApi:
    async def seed_logs(self, client):
        db = client.app.state.db
        await stats_repo.log_call(db, kind="hit", question="第一题")
        await stats_repo.log_call(db, kind="miss", question="第二题", latency_ms=100)
        await stats_repo.log_call(db, kind="llm-fail", question="第三题", error="boom")

    async def test_default_limit_and_newest_first(self, client):
        await self.seed_logs(client)
        resp = await client.get("/api/log")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [item["kind"] for item in items] == ["llm-fail", "miss", "hit"]
        assert items[0]["question"] == "第三题"

    async def test_kind_filter(self, client):
        await self.seed_logs(client)
        items = (await client.get("/api/log", params={"kind": "hit"})).json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "hit"

    async def test_limit_bounds(self, client):
        await self.seed_logs(client)
        assert len((await client.get("/api/log", params={"limit": 2})).json()["items"]) == 2
        assert (await client.get("/api/log", params={"limit": 501})).status_code == 422
        assert (await client.get("/api/log", params={"limit": 0})).status_code == 422

    async def test_empty_log(self, client):
        body = (await client.get("/api/log")).json()
        assert body == {"items": []}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/providers"),
        ("GET", "/api/providers/status"),
        ("POST", "/api/providers"),
        ("GET", "/api/cache"),
        ("GET", "/api/cache/export"),
        ("GET", "/api/stats"),
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
        ("GET", "/api/log"),
    ],
)
async def test_all_admin_endpoints_require_token(client, method, path):
    await SettingsRepo(client.app.state.db).put({"api_token": "secret"})
    resp = await client.request(method, path, json={})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
