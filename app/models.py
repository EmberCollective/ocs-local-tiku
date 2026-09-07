"""领域模型：frozen dataclass，字段与 questions 表对应。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NewQuestion:
    """待写入缓存的题目（LLM 答题成功或导入）。"""

    cache_key: str
    question: str
    qtype: str
    options: list[str]
    answer: str
    source: str = "llm"


@dataclass(frozen=True)
class QuestionRecord(NewQuestion):
    """缓存中的完整题目行。"""

    id: int = 0
    created_at: int = 0
    last_hit_at: int = 0
    hits: int = 0


@dataclass(frozen=True)
class Provider:
    """LLM 提供方（OpenAI 兼容端点）。"""

    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    priority: int = 1
    rpm: int | None = None
    max_parallel: int | None = None
    enabled: bool = True
    created_at: int = 0
    updated_at: int = 0
