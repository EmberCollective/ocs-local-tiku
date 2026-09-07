"""API 层依赖注入：AppState 访问器（M2 增加可选 token 校验）。"""

from fastapi import Request

from app.answer.service import AnswerService


def get_answer_service(request: Request) -> AnswerService:
    return request.app.state.answer_service
