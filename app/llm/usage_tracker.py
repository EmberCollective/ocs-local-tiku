"""litellm CustomLogger：按 api_base 归属 provider，回调写 provider 维度统计。

litellm 回调的 kwargs["litellm_params"] 可能是对象或 dict，统一取属性兼容。
"""

import logging

from litellm import CustomLogger

from app.llm.types import read_usage

logger = logging.getLogger(__name__)


class UsageTracker(CustomLogger):
    def __init__(self, api_base_map: dict[str, str], on_success, on_failure) -> None:
        # on_success(provider_id, prompt_tokens, completion_tokens) / on_failure(provider_id)
        self._api_base_map = api_base_map
        self._on_success = on_success
        self._on_failure = on_failure

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        provider_id = self._provider_id(kwargs)
        if provider_id is None or _is_test_call(kwargs) or self._on_success is None:
            return
        prompt_tokens, completion_tokens = read_usage(response_obj)
        try:
            await self._on_success(provider_id, prompt_tokens, completion_tokens)
        except Exception:  # 统计失败不干扰主流程
            logger.exception("usage tracker 回调失败")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        provider_id = self._provider_id(kwargs)
        if provider_id is None or _is_test_call(kwargs) or self._on_failure is None:
            return
        try:
            await self._on_failure(provider_id)
        except Exception:
            logger.exception("usage tracker 回调失败")

    def _provider_id(self, kwargs: dict) -> str | None:
        api_base = _get(kwargs.get("litellm_params"), "api_base")
        return self._api_base_map.get(api_base)


def _is_test_call(kwargs: dict) -> bool:
    """带 tiku_test 标记的直连冒烟，不计入 provider 统计。"""
    metadata = kwargs.get("metadata") or _get(kwargs.get("litellm_params"), "metadata") or {}
    return isinstance(metadata, dict) and bool(metadata.get("tiku_test"))


def _get(params, name: str):
    """对象或 dict 兼容取值。"""
    if params is None:
        return None
    if isinstance(params, dict):
        return params.get(name)
    return getattr(params, name, None)
