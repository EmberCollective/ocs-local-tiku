"""查询主流程：缓存命中/未命中、in-flight 同题去重、失败不缓存。

数据流（design §5）：
命中 → touch + 统计 + 内容→字母映射 → code=1
未命中 → in-flight 共享 Task → LLM → 解析 → 写缓存 → code=1
LLM 失败/解析失败 → code=0，绝不缓存（防错误答案固化）
先答后写：缓存写失败仅记日志，不影响已生成的答案返回。
"""

import asyncio
import logging
from dataclasses import dataclass

from app.answer.parse import ParseError, content_to_response, parse_llm_reply
from app.answer.prompt import build_messages
from app.db import Database
from app.llm.types import AskResult, LLMProtocol
from app.models import NewQuestion
from app.normalize import build_cache_key, canonical_qtype, split_options
from app.repository import questions, stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryOutcome:
    """查询结果：kind 决定 API 层如何渲染 code/msg。"""

    ok: bool
    kind: str  # hit | miss | llm-fail | parse-fail
    question: str
    answer: str
    msg: str = ""


class AnswerService:
    def __init__(self, db: Database, llm: LLMProtocol) -> None:
        self._db = db
        self._llm = llm
        self._inflight: dict[str, asyncio.Task[QueryOutcome]] = {}

    async def query(
        self, title: str, qtype_raw: str | None, options_raw: str | None
    ) -> QueryOutcome:
        qtype = canonical_qtype(qtype_raw)
        options = split_options(options_raw)
        key = build_cache_key(title, qtype, options)

        cached = await questions.get_by_key(self._db, key)
        if cached is not None:
            return await self._record_hit(key, title, cached.answer, qtype, options)

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._answer_and_cache(title, qtype, options, key))
            task.add_done_callback(lambda finished, k=key: self._inflight.pop(k, None))
            self._inflight[key] = task
        # shield：任一等待方被取消（OCS 60s 超时放弃）不影响任务完成并写缓存
        return await asyncio.shield(task)

    async def _record_hit(
        self, key: str, title: str, content: str, qtype: str, options: list[str]
    ) -> QueryOutcome:
        day = stats.today()
        try:
            await asyncio.gather(
                stats.bump_daily(self._db, day, cache_hits=1),
                stats.log_call(self._db, kind="hit", cache_key=key, question=title),
                questions.touch(self._db, key),
            )
        except Exception:
            logger.exception("命中路径统计/更新失败（不影响返回）cache_key=%s", key)
        return QueryOutcome(
            ok=True, kind="hit", question=title,
            answer=content_to_response(content, qtype, options),
        )

    async def _answer_and_cache(
        self, title: str, qtype: str, options: list[str], key: str
    ) -> QueryOutcome:
        day = stats.today()
        await stats.bump_daily(self._db, day, cache_misses=1)
        messages = build_messages(title, qtype, options)

        try:
            result = await self._llm.ask(messages)
        except Exception as error:
            await asyncio.gather(
                stats.bump_daily(self._db, day, llm_calls=1, llm_failures=1),
                stats.log_call(
                    self._db, kind="llm-fail", cache_key=key, question=title, error=str(error)
                ),
            )
            logger.warning("LLM 调用失败 cache_key=%s: %s", key, error)
            return QueryOutcome(
                ok=False, kind="llm-fail", question=title, answer="",
                msg=f"LLM 全部不可用：{error}",
            )

        try:
            content = parse_llm_reply(result.content, qtype, options)
        except ParseError as error:
            await asyncio.gather(
                stats.bump_daily(self._db, day, llm_calls=1),
                self._log_llm_call("parse-fail", key, title, result, error=str(error)),
            )
            return QueryOutcome(
                ok=False, kind="parse-fail", question=title, answer="", msg="答案解析失败",
            )

        await asyncio.gather(
            stats.bump_daily(
                self._db, day, llm_calls=1,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            ),
            self._log_llm_call("miss", key, title, result),
        )
        try:
            await questions.insert_question(
                self._db,
                NewQuestion(
                    cache_key=key, question=title, qtype=qtype,
                    options=options, answer=content, source="llm",
                ),
            )
        except Exception:
            logger.exception("缓存写入失败（先答后写，不影响返回）cache_key=%s", key)

        return QueryOutcome(
            ok=True, kind="miss", question=title,
            answer=content_to_response(content, qtype, options),
        )

    async def _log_llm_call(
        self, kind: str, key: str, title: str, result: AskResult, error: str | None = None
    ) -> None:
        """写一条携带 result 派生字段的调用日志（error=None 落库 NULL）。"""
        await stats.log_call(
            self._db, kind=kind, cache_key=key, question=title, error=error,
            provider_id=result.provider_id, model=result.model,
            latency_ms=result.latency_ms, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
