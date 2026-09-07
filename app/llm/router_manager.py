"""litellm Router 封装：providers/settings → Router 构建、ask、直连测试。

litellm 仅在本模块与 usage_tracker 内出现；遥测在 import litellm 前关闭。
"""

import os
import time

os.environ.setdefault("LITELLM_TELEMETRY", "False")

from litellm import Router, acompletion  # noqa: E402

from app.db import Database
from app.llm.types import AskResult
from app.llm.usage_tracker import UsageTracker
from app.models import Provider
from app.repository import stats as stats_repo

MODEL_GROUP = "answering"
TEST_TIMEOUT_SECONDS = 10
TEST_MESSAGES = [{"role": "user", "content": "ping，请回复 pong"}]


class LLMUnavailable(Exception):
    """Router 未构建或全部 deployment 调用失败。"""


def build_model_list(providers: list[Provider], settings: dict) -> list[dict]:
    """providers → litellm model_list：同组名多 deployment，order=优先级。"""
    model_list = []
    for provider in sorted(providers, key=lambda p: p.priority):
        litellm_params = {
            "model": f"openai/{provider.model}",
            "api_base": provider.base_url,
            "api_key": provider.api_key,
            "order": provider.priority,
        }
        if provider.rpm:
            litellm_params["rpm"] = provider.rpm
        if provider.max_parallel:
            litellm_params["max_parallel_requests"] = provider.max_parallel
        model_list.append({"model_name": MODEL_GROUP, "litellm_params": litellm_params})
    return model_list


class RouterManager:
    def __init__(self, db: Database, *, list_enabled, settings) -> None:
        self._db = db
        self._list_enabled = list_enabled
        self._settings = settings
        self._router: Router | None = None

    async def rebuild(self) -> None:
        """按当前 providers+settings 重建 Router；无可用 provider 时置空。"""
        providers = await self._list_enabled()
        settings = await self._settings.get_all()
        model_list = build_model_list(providers, settings)
        if not model_list:
            self._router = None
            return
        self._router = Router(
            model_list=model_list,
            routing_strategy=settings["routing_strategy"],
            num_retries=settings["num_retries"],
            allowed_fails=settings["allowed_fails"],
            cooldown_time=settings["cooldown_time"],
            default_litellm_params={
                "temperature": 0,
                "timeout": settings["llm_timeout"],
            },
            callbacks=[
                UsageTracker(
                    api_base_map={p.base_url: p.id for p in providers},
                    on_success=self._on_provider_success,
                    on_failure=self._on_provider_failure,
                )
            ],
        )

    async def ask(self, messages: list[dict]) -> AskResult:
        if self._router is None:
            raise LLMUnavailable("未配置可用的 LLM provider")
        start = time.monotonic()
        try:
            response = await self._router.acompletion(model=MODEL_GROUP, messages=messages)
        except Exception as error:  # litellm 各类异常聚合为摘要
            raise LLMUnavailable(f"{type(error).__name__}: {error}") from error
        usage = getattr(response, "usage", None)
        return AskResult(
            content=response["choices"][0]["message"]["content"] or "",
            model=response.get("model", ""),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def test_provider(self, provider: Provider) -> dict:
        """直连单 provider 冒烟（不经过 Router），timeout=10。"""
        start = time.monotonic()
        try:
            response = await acompletion(
                model=f"openai/{provider.model}",
                api_base=provider.base_url,
                api_key=provider.api_key,
                messages=TEST_MESSAGES,
                timeout=TEST_TIMEOUT_SECONDS,
                temperature=0,
            )
        except Exception as error:
            return {
                "ok": False,
                "latency_ms": int((time.monotonic() - start) * 1000),
                "reply": None,
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "reply": response["choices"][0]["message"]["content"],
            "error": None,
        }

    def cooldowns(self) -> list[str]:
        """冷却中的 deployment 标记（best-effort，供状态页）。"""
        if self._router is None:
            return []
        try:
            return list(self._router.get_cooldown_deployments())
        except Exception:
            return []

    async def _on_provider_success(self, provider_id, prompt_tokens, completion_tokens) -> None:
        await stats_repo.bump_provider_daily(
            self._db,
            stats_repo.today(),
            provider_id,
            calls=1,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _on_provider_failure(self, provider_id) -> None:
        await stats_repo.bump_provider_daily(
            self._db, stats_repo.today(), provider_id, failures=1
        )
