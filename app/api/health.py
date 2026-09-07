"""免鉴权探活端点（healthcheck 用）。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}
