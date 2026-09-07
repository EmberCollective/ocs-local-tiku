"""OCS 对接端点：恒 HTTP 200，业务失败以 code=0 表达（design §7/§12）。"""

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from app.answer.service import AnswerService, QueryOutcome
from app.api.deps import get_answer_service
from app.normalize import VALID_QTYPES

logger = logging.getLogger(__name__)

router = APIRouter()


class QueryBody(BaseModel):
    """POST JSON body（OCS contentType=json 模式）。"""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    type: str | None = None
    options: str | None = None


@router.get("/api/query")
async def query_get(
    title: str = Query(default=""),
    type: str | None = Query(default=None),
    options: str | None = Query(default=None),
    service: AnswerService = Depends(get_answer_service),
) -> dict:
    return await _run(service, title, type, options)


@router.post("/api/query")
async def query_post(
    body: QueryBody | None = None,
    title: str = Query(default=""),
    type: str | None = Query(default=None),
    options: str | None = Query(default=None),
    service: AnswerService = Depends(get_answer_service),
) -> dict:
    if body is None:
        body = QueryBody()
    merged_title = body.title or title
    merged_type = body.type or type
    merged_options = body.options or options
    return await _run(service, merged_title, merged_type, merged_options)


async def _run(service: AnswerService, title: str, qtype: str | None, options: str | None) -> dict:
    if not title or not title.strip():
        return {"code": 0, "msg": "缺少题目"}
    _warn_invalid_type(qtype)
    try:
        outcome: QueryOutcome = await service.query(title, qtype, options)
    except Exception:
        logger.exception("/api/query 未预期异常 title=%r", title[:80])
        return {"code": 0, "msg": "服务内部错误"}
    if not outcome.ok:
        return {"code": 0, "msg": outcome.msg}
    return {"code": 1, "question": outcome.question, "answer": outcome.answer}


def _warn_invalid_type(qtype: str | None) -> None:
    if qtype and qtype.strip().casefold() not in VALID_QTYPES:
        logger.warning("非法题型 %r，回退 single", qtype)
