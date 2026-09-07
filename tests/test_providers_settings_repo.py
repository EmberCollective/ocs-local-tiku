"""providers 与 settings 仓储：CRUD、reorder、打码、DEFAULTS 合并。"""

import pytest

from app.repository import providers as providers_repo
from app.repository.settings import DEFAULTS, SettingsRepo

pytestmark = pytest.mark.asyncio


def make_payload(**overrides) -> dict:
    payload = {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-abc123def456",
        "model": "deepseek-chat",
    }
    return {**payload, **overrides}


async def test_create_and_list(db):
    await providers_repo.create_provider(db, make_payload())
    await providers_repo.create_provider(db, make_payload(name="Kimi", model="moonshot-v1-8k"))
    listed = await providers_repo.list_providers(db)
    assert len(listed) == 2
    assert {p.name for p in listed} == {"DeepSeek", "Kimi"}


async def test_create_assigns_uuid_and_priority(db):
    created = await providers_repo.create_provider(db, make_payload())
    assert len(created.id) == 32  # uuid4().hex
    assert created.priority == 1
    assert created.enabled is True

    second = await providers_repo.create_provider(db, make_payload(name="第二"))
    assert second.priority == 2  # 追加到末尾


async def test_update_keeps_blank_api_key(db):
    created = await providers_repo.create_provider(db, make_payload())
    await providers_repo.update_provider(db, created.id, {"name": "改名", "api_key": ""})
    record = await providers_repo.get_provider(db, created.id)
    assert record.name == "改名"
    assert record.api_key == "sk-abc123def456"  # 留空 = 不修改


async def test_update_changes_api_key_when_provided(db):
    created = await providers_repo.create_provider(db, make_payload())
    await providers_repo.update_provider(db, created.id, {"api_key": "sk-new"})
    record = await providers_repo.get_provider(db, created.id)
    assert record.api_key == "sk-new"


async def test_delete(db):
    created = await providers_repo.create_provider(db, make_payload())
    assert await providers_repo.delete_provider(db, created.id) is True
    assert await providers_repo.get_provider(db, created.id) is None
    assert await providers_repo.delete_provider(db, "missing") is False


async def test_reorder_rewrites_priority_by_position(db):
    first = await providers_repo.create_provider(db, make_payload())
    second = await providers_repo.create_provider(db, make_payload(name="Kimi"))
    third = await providers_repo.create_provider(db, make_payload(name="GLM"))

    await providers_repo.reorder(db, [third.id, first.id, second.id])
    listed = await providers_repo.list_providers(db)  # 按 priority 排序
    assert [p.name for p in listed] == ["GLM", "DeepSeek", "Kimi"]
    assert [p.priority for p in listed] == [1, 2, 3]


async def test_reorder_missing_id_raises(db):
    created = await providers_repo.create_provider(db, make_payload())
    with pytest.raises(ValueError):
        await providers_repo.reorder(db, [created.id, "missing-id"])


async def test_set_enabled(db):
    created = await providers_repo.create_provider(db, make_payload())
    await providers_repo.set_enabled(db, created.id, False)
    record = await providers_repo.get_provider(db, created.id)
    assert record.enabled is False
    enabled_only = await providers_repo.list_enabled(db)
    assert enabled_only == []


class TestSettingsRepo:
    async def test_get_missing_key_returns_default(self, db):
        repo = SettingsRepo(db)
        assert await repo.get("ttl_days") == DEFAULTS["ttl_days"] == 60

    async def test_get_all_merges_defaults(self, db):
        repo = SettingsRepo(db)
        await repo.put({"ttl_days": 30})
        values = await repo.get_all()
        assert values["ttl_days"] == 30
        assert values["cleanup_batch_size"] == 500  # 未改的键自动默认

    async def test_put_persists_json(self, db):
        repo = SettingsRepo(db)
        await repo.put({"api_token": "secret", "routing_strategy": "least-busy"})
        assert await repo.get("api_token") == "secret"
        assert await repo.get("routing_strategy") == "least-busy"

    async def test_put_ignores_unknown_keys(self, db):
        repo = SettingsRepo(db)
        await repo.put({"ttl_days": 7, "not_a_setting": 1})
        assert await repo.get("ttl_days") == 7
        assert await repo.get("not_a_setting") is None
