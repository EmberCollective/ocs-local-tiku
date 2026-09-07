"""批量导入端点（design §7）：JSON body ≤5000 条/批；文件导入支持 JSON 与 ZIP。

文件导入护栏：上传体积、zip 条目数、解压尺寸均有上限，超限直接拒绝不解压。
超 5000 条自动分批 upsert（导出→导入闭环不受单批上限约束）。
"""

import io
import json
import logging
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.api.deps import get_db
from app.db import Database
from app.repository import questions, stats

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_IMPORT_ITEMS = 5000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 上传体积上限（zip 为压缩后尺寸）
MAX_ZIP_JSON_ENTRIES = 100  # zip 内 .json 条目数上限
MAX_ENTRY_UNCOMPRESSED_BYTES = 64 * 1024 * 1024  # zip 单条目解压尺寸上限
MAX_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024  # zip 全部条目解压总量上限


class ImportFileError(ValueError):
    """导入文件解析失败（内容格式不合法或护栏拦截）。"""


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


_import_items_adapter = TypeAdapter(list[ImportItem])


@router.post("/api/import")
async def import_items(body: ImportBody, db: Database = Depends(get_db)) -> dict:
    if len(body.items) > MAX_IMPORT_ITEMS:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_IMPORT_ITEMS} 条")
    items = [item.model_dump() for item in body.items]
    return await _run_import(db, items)


@router.post("/api/import/file")
async def import_file(
    file: UploadFile = File(...), db: Database = Depends(get_db)
) -> dict:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限"
        )
    try:
        raw_items = extract_import_items(data, filename=file.filename or "")
        items = [item.model_dump() for item in _import_items_adapter.validate_python(raw_items)]
    except ImportFileError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except ValidationError:
        raise HTTPException(
            status_code=400, detail="存在字段类型非法的条目（question/answer/options）"
        ) from None
    if not items:
        raise HTTPException(status_code=400, detail="文件中没有可导入的条目")
    return await _run_import(db, items)


async def _run_import(db: Database, items: list[dict]) -> dict:
    """分批 upsert + 导入日志；批间无事务边界，单批失败整体 500（与现状一致）。"""
    imported = updated = skipped = 0
    try:
        for start in range(0, len(items), MAX_IMPORT_ITEMS):
            chunk = items[start : start + MAX_IMPORT_ITEMS]
            delta = await questions.upsert_import(db, chunk)
            imported, updated, skipped = (
                imported + delta[0],
                updated + delta[1],
                skipped + delta[2],
            )
    except Exception:
        logger.exception("导入失败（%d 条）", len(items))
        raise HTTPException(status_code=500, detail="导入失败") from None
    try:
        await stats.log_call(db, kind="import", error=None)
    except Exception:
        logger.exception("导入日志写入失败（不影响返回）")
    return {"code": 1, "imported": imported, "updated": updated, "skipped": skipped}


def extract_import_items(data: bytes, *, filename: str = "") -> list[dict]:
    """上传内容 → 待导入条目：zip（文件名后缀或内容判定）逐 .json 条目解析，否则按 JSON 文本。"""
    if filename.lower().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(data)):
        return _items_from_zip(data)
    return _items_from_json(data, label=filename or "文件")


def _items_from_zip(data: bytes) -> list[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".json")
            ]
            _guard_zip_infos(infos)
            return [
                item
                for info in infos
                for item in _items_from_json(archive.read(info.filename), label=info.filename)
            ]
    except zipfile.BadZipFile as error:
        raise ImportFileError("不是有效的 ZIP 文件") from error


def _guard_zip_infos(infos: list[zipfile.ZipInfo]) -> None:
    """zip 炸弹护栏：按 ZipFile 头声明的解压尺寸拦截，超限不实际解压。"""
    if not infos:
        raise ImportFileError("ZIP 内没有 JSON 文件")
    if len(infos) > MAX_ZIP_JSON_ENTRIES:
        raise ImportFileError(f"ZIP 内 JSON 文件超过 {MAX_ZIP_JSON_ENTRIES} 个上限")
    if any(info.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES for info in infos):
        raise ImportFileError(
            f"ZIP 内存在解压后超过 {MAX_ENTRY_UNCOMPRESSED_BYTES // (1024 * 1024)} MB 的条目"
        )
    if sum(info.file_size for info in infos) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ImportFileError(
            f"ZIP 解压总量超过 {MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB 上限"
        )


def _items_from_json(data: bytes, *, label: str) -> list[dict]:
    """单份 JSON 文本 → 条目数组；兼容 {"items": [...]} 包装、裸数组、BOM 与字符串内裸控制字符。"""
    try:
        # strict=False：容忍手改文件时字符串里遗留的裸换行等控制字符
        parsed = json.loads(data.decode("utf-8-sig"), strict=False)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportFileError(f"{label} 不是有效的 UTF-8 JSON") from error
    items = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise ImportFileError(f'{label} 应为题目数组或 {{"items": [...]}} 格式')
    return items
