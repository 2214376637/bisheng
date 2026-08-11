from fastapi import APIRouter, Depends, Request

from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.schemas.api import resp_200
from bisheng.teaching_research.schemas import AchievementExtractReq
from bisheng.teaching_research.service import AchievementExtractService


router = APIRouter(prefix="/knowledge/achievement", tags=["TeachingResearchAchievement"])


@router.post("/extract")
async def extract_achievement(
        *,
        request: Request,
        login_user: UserPayload = Depends(UserPayload.get_login_user),
        req_data: AchievementExtractReq,
):
    result = await AchievementExtractService.extract(login_user, req_data)
    return resp_200(result)

