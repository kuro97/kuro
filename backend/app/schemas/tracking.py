import uuid
from datetime import datetime

from pydantic import BaseModel


class SourceStats(BaseModel):
    """Статистика по источнику трафика."""

    source: str
    total: int
    answered: int
    qualified: int
    paid: int
    revenue: int


class CityStats(BaseModel):
    """Статистика по городу (из AMO CRM)."""

    city: str
    total: int
    qualified: int
    paid: int
    revenue: int


class DayStats(BaseModel):
    """Статистика за один день (для линейного графика)."""

    day: str  # YYYY-MM-DD
    total: int
    qualified: int
    paid: int


class StatsResponse(BaseModel):
    """Агрегированная статистика по звонкам за период."""

    total: int           # уникальных звонков (по linkedid, дедупликация)
    total_attempts: int  # всего legs (без дедупликации)
    answered: int
    qualified: int
    paid: int
    revenue: int
    qualified_pct: float
    paid_pct: float
    by_source: list[SourceStats]
    by_city: list[CityStats]
    by_day: list[DayStats]


class GetNumberRequest(BaseModel):
    """Запрос от JS-скрипта на получение подменного номера."""

    client_id: str
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    keyword: str | None = None
    content: str | None = None
    gclid: str | None = None
    referrer: str | None = None
    landing_page: str | None = None


class GetNumberResponse(BaseModel):
    """Ответ с подменным номером."""

    phone: str
    session_id: str
    heartbeat_interval: int = 30


class HeartbeatRequest(BaseModel):
    session_id: str


class HeartbeatResponse(BaseModel):
    ok: bool


class CallOut(BaseModel):
    id: uuid.UUID
    # project_id=None означает неатрибуцированный звонок (номер не найден в пуле)
    project_id: uuid.UUID | None = None
    caller_number: str
    tracking_did: str
    target_number: str | None
    answered_by: str | None
    started_at: datetime
    duration: int
    billsec: int
    disposition: str
    is_unique: bool
    is_target: bool
    source: str | None
    medium: str | None
    campaign: str | None
    keyword: str | None
    amo_city: str | None = None
    recording_url: str | None

    model_config = {"from_attributes": True}


class CallStats(BaseModel):
    total_calls: int
    answered_calls: int
    missed_calls: int
    unique_calls: int
    target_calls: int
    avg_duration: float
    answer_rate: float


class PoolStats(BaseModel):
    free: int
    busy: int
    total: int
    utilization: float
