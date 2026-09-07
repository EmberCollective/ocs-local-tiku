"""管理 API：缓存分页/搜索/删除/导出 + 统计摘要（design §7）。"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import get_db
from app.db import Database
from app.models import QuestionRecord
from app.repository import questions as questions_repo
from app.repository import stats as stats_repo

router = APIRouter()

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
MAX_BATCH_DELETE = 1000
DAILY_SERIES_DAYS = 14


class DeleteBatchBody(BaseModel):
    """批量删除请求体：单批上限 MAX_BATCH_DELETE。"""

    ids: list[int]


@router.get("/api/cache")
async def list_cache(
    q: str | None = None,
    type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Database = Depends(get_db),
) -> dict:
    records, total = await questions_repo.list_page(
        db, q=q, qtype=type, page=page, page_size=page_size
    )
    return {
        "items": [_serialize(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/cache/export")
async def export_cache(db: Database = Depends(get_db)) -> Response:
    """导出全部缓存：attachment 头让浏览器直接下载（可直接再导入面板）。"""
    payload = json.dumps(
        {"items": await questions_repo.export_all(db)}, ensure_ascii=False
    )
    filename = f"tiku-export-{datetime.now():%Y%m%d-%H%M%S}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/cache/{question_id}")
async def delete_cache(question_id: int, db: Database = Depends(get_db)) -> dict:
    deleted = await questions_repo.delete_by_ids(db, [question_id])
    if deleted == 0:
        raise HTTPException(status_code=404, detail="缓存条目不存在")
    return {"deleted": deleted}


@router.post("/api/cache/delete-batch")
async def delete_cache_batch(body: DeleteBatchBody, db: Database = Depends(get_db)) -> dict:
    if len(body.ids) > MAX_BATCH_DELETE:
        raise HTTPException(status_code=400, detail=f"单次最多删除 {MAX_BATCH_DELETE} 条")
    return {"deleted": await questions_repo.delete_by_ids(db, body.ids)}


@router.get("/api/stats")
async def get_stats(db: Database = Depends(get_db)) -> dict:
    """今日 + 累计 + 近 14 天序列；空档日期补零。"""
    days = [stats_repo.days_ago(offset) for offset in range(DAILY_SERIES_DAYS - 1, -1, -1)]
    rows = await stats_repo.daily_between(db, days[0], days[-1])
    by_day = {row["day"]: dict(row) for row in rows}
    daily = [by_day.get(day, _empty_day(day)) for day in days]
    totals = await stats_repo.daily_totals(db)
    return {
        "today": daily[-1],
        "total": {**totals, "questions": await questions_repo.count_all(db)},
        "daily": daily,
    }


def _serialize(record: QuestionRecord) -> dict:
    return {
        "id": record.id,
        "cache_key": record.cache_key,
        "question": record.question,
        "type": record.qtype,
        "options": record.options,
        "answer": record.answer,
        "source": record.source,
        "created_at": record.created_at,
        "last_hit_at": record.last_hit_at,
        "hits": record.hits,
    }


def _empty_day(day: str) -> dict:
    return {"day": day, **{field: 0 for field in stats_repo.DAILY_FIELDS}}
