"""litellm CustomLogger：按 api_base 归属 provider，回调写 provider 维度统计。

litellm 回调的 kwargs["litellm_params"] 可能是对象或 dict，统一取属性兼容。
"""

import logging

from litellm import CustomLogger

logger = logging.getLogger(__name__)


class UsageTracker(CustomLogger):
    def __init__(self, api_base_map: dict[str, str], on_success, on_failure) -> None:
        # on_success(provider_id, prompt_tokens, completion_tokens) / on_failure(provider_id)
        self._api_base_map = api_base_map
        self._on_success = on_success
        self._on_failure = on_failure

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        provider_id = self._provider_id(kwargs)
        if provider_id is None or self._on_success is None:
            return
        usage = getattr(response_obj, "usage", None)
        try:
            await self._on_success(
                provider_id,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )
        except Exception:  # 统计失败不干扰主流程
            logger.exception("usage tracker 回调失败")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        provider_id = self._provider_id(kwargs)
        if provider_id is None or self._on_failure is None:
            return
        try:
            await self._on_failure(provider_id)
        except Exception:
            logger.exception("usage tracker 回调失败")

    def _provider_id(self, kwargs: dict) -> str | None:
        api_base = _get(kwargs.get("litellm_params"), "api_base")
        return self._api_base_map.get(api_base)


def _get(params, name: str):
    """对象或 dict 兼容取值。"""
    if params is None:
        return None
    if isinstance(params, dict):
        return params.get(name)
    return getattr(params, name, None)
