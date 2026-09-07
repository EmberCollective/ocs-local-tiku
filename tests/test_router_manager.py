"""litellm Router 集成：model_list 构造、RouterManager rebuild/ask/test、用量追踪。

全部用替身注入，不联网；真实调用留 @pytest.mark.live（默认 skip）。
"""

import os
from types import SimpleNamespace

import pytest

import app.llm.router_manager as rm
from app.llm.router_manager import LLMUnavailable, RouterManager, build_model_list
from app.llm.usage_tracker import UsageTracker
from app.models import Provider
from app.repository.settings import DEFAULTS


def make_provider(pid="p1", priority=1, rpm=None, max_parallel=None,
                  base_url="https://api.x.com/v1", model="test-model") -> Provider:
    return Provider(
        id=pid, name=f"provider-{pid}", base_url=base_url, api_key="sk-x",
        model=model, priority=priority, rpm=rpm, max_parallel=max_parallel,
    )


class FakeSettings:
    def __init__(self, values=None):
        self.values = {**DEFAULTS, **(values or {})}

    async def get_all(self):
        return self.values


def make_list_enabled(providers):
    async def list_enabled():
        return providers

    return list_enabled


def test_telemetry_disabled():
    assert os.environ.get("LITELLM_TELEMETRY") == "False"


class FakeUsage:
    prompt_tokens = 11
    completion_tokens = 7


class FakeResponse(dict):
    def __init__(self, content="答案文本"):
        super().__init__(
            model="gpt-fake",
            choices=[{"message": {"content": content}}],
        )
        self.usage = FakeUsage()


class TestBuildModelList:
    def test_sorts_by_priority_and_maps_fields(self):
        providers = [make_provider(pid="p2", priority=2), make_provider(pid="p1", priority=1)]
        model_list = build_model_list(providers, dict(DEFAULTS))
        assert [m["litellm_params"]["order"] for m in model_list] == [1, 2]
        assert model_list[0]["model_name"] == "answering"
        assert model_list[0]["litellm_params"]["model"] == "openai/test-model"
        assert model_list[0]["litellm_params"]["api_base"] == "https://api.x.com/v1"
        assert model_list[0]["litellm_params"]["api_key"] == "sk-x"

    def test_conditional_params(self):
        with_limits = build_model_list(
            [make_provider(rpm=60, max_parallel=4)], dict(DEFAULTS)
        )[0]["litellm_params"]
        assert with_limits["rpm"] == 60
        assert with_limits["max_parallel_requests"] == 4

        without = build_model_list([make_provider()], dict(DEFAULTS))[0]["litellm_params"]
        assert "rpm" not in without
        assert "max_parallel_requests" not in without

    def test_empty_providers_returns_empty(self):
        assert build_model_list([], dict(DEFAULTS)) == []


class TestRouterManager:
    pytestmark = pytest.mark.asyncio
    async def test_rebuild_without_providers_disables_router(self, db):
        manager = RouterManager(db, list_enabled=make_list_enabled([]), settings=FakeSettings())
        await manager.rebuild()
        with pytest.raises(LLMUnavailable):
            await manager.ask([{"role": "user", "content": "hi"}])

    async def test_rebuild_passes_router_kwargs(self, db, monkeypatch):
        captured = {}

        class FakeRouter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(rm, "Router", FakeRouter)
        manager = RouterManager(
            db,
            list_enabled=make_list_enabled([make_provider()]),
            settings=FakeSettings({"routing_strategy": "least-busy", "num_retries": 2}),
        )
        await manager.rebuild()
        assert captured["routing_strategy"] == "least-busy"
        assert captured["num_retries"] == 2
        assert captured["allowed_fails"] == DEFAULTS["allowed_fails"]
        assert captured["cooldown_time"] == DEFAULTS["cooldown_time"]
        assert captured["default_litellm_params"]["temperature"] == 0
        assert captured["default_litellm_params"]["timeout"] == DEFAULTS["llm_timeout"]
        assert len(captured["model_list"]) == 1

    async def test_ask_extracts_askresult(self, db, monkeypatch):
        class FakeRouter:
            def __init__(self, **kwargs):
                pass

            async def acompletion(self, **kwargs):
                assert kwargs["model"] == "answering"
                return FakeResponse("正确")

        monkeypatch.setattr(rm, "Router", FakeRouter)
        manager = RouterManager(
            db, list_enabled=make_list_enabled([make_provider()]), settings=FakeSettings()
        )
        await manager.rebuild()
        result = await manager.ask([{"role": "user", "content": "题目"}])
        assert result.content == "正确"
        assert result.model == "gpt-fake"
        assert result.prompt_tokens == 11
        assert result.completion_tokens == 7
        assert result.latency_ms >= 0

    async def test_ask_wraps_exception_as_llm_unavailable(self, db, monkeypatch):
        class FakeRouter:
            def __init__(self, **kwargs):
                pass

            async def acompletion(self, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(rm, "Router", FakeRouter)
        manager = RouterManager(
            db, list_enabled=make_list_enabled([make_provider()]), settings=FakeSettings()
        )
        await manager.rebuild()
        with pytest.raises(LLMUnavailable, match="boom"):
            await manager.ask([{"role": "user", "content": "题目"}])

    async def test_test_provider_ok(self, monkeypatch):
        async def fake_acompletion(**kwargs):
            assert kwargs["timeout"] == 10
            return FakeResponse("pong")

        monkeypatch.setattr(rm, "acompletion", fake_acompletion)
        manager = RouterManager(None, list_enabled=make_list_enabled([]), settings=FakeSettings())
        result = await manager.test_provider(make_provider())
        assert result["ok"] is True
        assert result["reply"] == "pong"
        assert result["error"] is None
        assert result["latency_ms"] >= 0

    async def test_test_provider_error(self, monkeypatch):
        async def fake_acompletion(**kwargs):
            raise RuntimeError("auth error")

        monkeypatch.setattr(rm, "acompletion", fake_acompletion)
        manager = RouterManager(None, list_enabled=make_list_enabled([]), settings=FakeSettings())
        result = await manager.test_provider(make_provider())
        assert result["ok"] is False
        assert "auth error" in result["error"]


class TestUsageTracker:
    pytestmark = pytest.mark.asyncio
    async def test_success_event_maps_api_base_to_provider(self):
        events = []

        async def on_success(provider_id, prompt_tokens, completion_tokens):
            events.append(("ok", provider_id, prompt_tokens, completion_tokens))

        async def on_failure(provider_id):
            events.append(("fail", provider_id))

        tracker = UsageTracker(
            api_base_map={"https://api.x.com/v1": "p1"},
            on_success=on_success,
            on_failure=on_failure,
        )
        await tracker.async_log_success_event(
            kwargs={"litellm_params": SimpleNamespace(api_base="https://api.x.com/v1")},
            response_obj=FakeResponse(),
            start_time=None,
            end_time=None,
        )
        assert events == [("ok", "p1", 11, 7)]

    async def test_failure_event(self):
        events = []

        async def on_success(provider_id, prompt_tokens, completion_tokens):
            events.append(("ok", provider_id))

        async def on_failure(provider_id):
            events.append(("fail", provider_id))

        tracker = UsageTracker(
            api_base_map={"https://api.x.com/v1": "p1"},
            on_success=on_success,
            on_failure=on_failure,
        )
        await tracker.async_log_failure_event(
            kwargs={"litellm_params": {"api_base": "https://api.x.com/v1"}},
            response_obj=None,
            start_time=None,
            end_time=None,
        )
        assert events == [("fail", "p1")]

    async def test_unknown_api_base_ignored(self):
        events = []

        async def on_failure(provider_id):
            events.append(provider_id)

        tracker = UsageTracker(api_base_map={}, on_success=None, on_failure=on_failure)
        await tracker.async_log_failure_event(
            kwargs={"litellm_params": SimpleNamespace(api_base="https://unknown/v1")},
            response_obj=None,
            start_time=None,
            end_time=None,
        )
        assert events == []


@pytest.mark.live
async def test_live_smoke_skipped_by_default():  # pragma: no cover
    """真实 provider 冒烟（--live 显式开启）。"""
