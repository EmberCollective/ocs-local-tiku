"""批量导入端点（design §7）：≤5000 条/批，同哈希键去重 upsert。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.db import Database
from app.repository import questions, stats

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_IMPORT_ITEMS = 5000


class ImportItem(BaseModel):
    """单条导入：question/answer 缺失或空白由仓储层计为 skipped（坏行不中断好行）。"""

    model_config = ConfigDict(extra="ignore")

    question: str | None = None
    type: str | None = None
    options: str | list[str] | None = None
    answer: str | None = None


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[ImportItem] = []


def _get_db(request: Request) -> Database:
    return request.app.state.db


@router.post("/api/import")
async def import_items(body: ImportBody, db: Database = Depends(_get_db)) -> dict:
    if len(body.items) > MAX_IMPORT_ITEMS:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_IMPORT_ITEMS} 条")
    items = [item.model_dump() for item in body.items]
    try:
        imported, updated, skipped = await questions.upsert_import(db, items)
    except Exception:
        logger.exception("/api/import 导入失败（%d 条）", len(items))
        raise HTTPException(status_code=500, detail="导入失败") from None
    try:
        await stats.log_call(db, kind="import", error=None)
    except Exception:
        logger.exception("导入日志写入失败（不影响返回）")
    return {"code": 1, "imported": imported, "updated": updated, "skipped": skipped}
