"""冒烟用 LLM 替身（仅测试/协议联通验证，不进生产路径）。"""

import logging

from app.llm.types import AskResult
from app.normalize import strip_option_prefix

logger = logging.getLogger(__name__)

_OPTION_MARKER = "选项："
_QTYPE_MARKER = "题型："
_COMPLETION_ECHO = "本地测试填空答案"


class EchoLLM:
    """回声 LLM：按 prompt 契约作答——选项题回第一选项、判断题回「正确」。

    供 TIKU_FAKE_LLM=1 时在不接真实 API 的情况下验证 OCS 协议联通。
    """

    async def ask(self, messages: list[dict]) -> AskResult:
        user_content = messages[-1]["content"]
        return AskResult(
            content=_echo_answer(user_content),
            model="echo",
            provider_id="echo",
            latency_ms=0,
        )


def _echo_answer(user_content: str) -> str:
    lines = user_content.split("\n")
    if any("判断题" in line for line in lines if line.startswith(_QTYPE_MARKER)):
        return "正确"
    if _OPTION_MARKER in lines:
        start = lines.index(_OPTION_MARKER) + 1
        options = [line for line in lines[start:] if line.strip()]
        if options:
            return strip_option_prefix(options[0])
    return _COMPLETION_ECHO
