import uuid
from datetime import datetime

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    domain: str
    default_phone: str


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    domain: str
    default_phone: str
    is_active: bool
    api_key: str
    created_at: datetime

    model_config = {"from_attributes": True}
