"""API для callback-виджета (обратный звонок).
Посетитель оставляет номер → система звонит менеджеру → соединяет с клиентом."""

from pydantic import BaseModel
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.project import Project
from app.services.ami_client import ami_client

router = APIRouter(prefix="/callback", tags=["callback"])


class CallbackRequest(BaseModel):
    phone: str  # номер клиента
    name: str | None = None
    source: str | None = None  # откуда виджет (utm_source)


class CallbackResponse(BaseModel):
    ok: bool
    message: str


async def _get_project_by_api_key(
    x_api_key: str = Header(...), db: AsyncSession = Depends(get_db)
) -> Project:
    result = await db.execute(
        select(Project).where(Project.api_key == x_api_key, Project.is_active)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return project


@router.post("/request", response_model=CallbackResponse)
async def request_callback(
    body: CallbackRequest,
    project: Project = Depends(_get_project_by_api_key),
):
    """Запрос обратного звонка. Asterisk звонит менеджеру, затем клиенту."""
    # Нормализуем номер
    phone = body.phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    try:
        # Originate: звоним на default_phone проекта (очередь/менеджер),
        # после ответа Asterisk перезвонит клиенту
        await ami_client.originate_call(
            number=phone,
            extension=project.default_phone,
            context="kurotrack-callback",
        )
        return CallbackResponse(ok=True, message="Callback initiated")
    except Exception as e:
        return CallbackResponse(ok=False, message=f"Failed to initiate callback: {e}")
