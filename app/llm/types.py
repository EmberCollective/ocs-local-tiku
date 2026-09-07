"""LLM 层公共类型：AskResult 与最小协议（测试替身注入点）。

AnswerService 只依赖「有 ask(messages) -> AskResult 的对象」，
litellm 只在 RouterManager 内部出现（design §8）。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AskResult:
    """一次 LLM 调用的结果。"""

    content: str
    model: str
    provider_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class LLMProtocol(Protocol):
    async def ask(self, messages: list[dict]) -> AskResult: ...
