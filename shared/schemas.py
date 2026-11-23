
from pydantic import BaseModel, Field, model_validator
from typing import Literal


class HelpRequest(BaseModel):
    request_id: str = Field(min_length=1)
    origin_fog: str = Field(min_length=1)
    required_piles: int = Field(gt=0)
    required_power_kw: float = Field(gt=0)


class HelpResponse(BaseModel):
    request_id: str
    origin_fog: str
    proposer_fog: str
    zone: str
    status: str
    available_piles: int
    available_power_kw: float
    reason: str


class HoldRequest(BaseModel):
    request_id: str = Field(min_length=1)
    origin_fog: str = Field(min_length=1)
    required_piles: int = Field(gt=0)
    required_power_kw: float = Field(gt=0)
    ttl_seconds: int = Field(default=120, ge=1, le=600)


class HoldResponse(BaseModel):
    hold_id: str
    request_id: str
    origin_fog: str
    proposer_fog: str
    pile_ids: list[str]
    status: Literal["HOLD"]
    expires_at: str


class ConfirmRequest(BaseModel):
    hold_id: str = Field(min_length=1)


class ConfirmResponse(BaseModel):
    hold_id: str
    request_id: str
    origin_fog: str
    proposer_fog: str
    pile_ids: list[str]
    status: Literal["CONFIRMED"]


class CancelRequest(BaseModel):
    hold_id: str = Field(min_length=1)


class CancelResponse(BaseModel):
    hold_id: str
    request_id: str
    origin_fog: str
    proposer_fog: str
    pile_ids: list[str]
    status: Literal["CANCELLED"]


class ActivateRequest(BaseModel):
    hold_id: str = Field(min_length=1)
    # Optional metadata for round-based remote charging. Legacy API remains valid.
    started_tick: int | None = Field(default=None, ge=1)
    duration_ticks: int | None = Field(default=None, ge=1, le=48)
    vehicle_id: str | None = None

    @model_validator(mode="after")
    def valid_duration_metadata(self):
        all_missing = self.started_tick is None and self.duration_ticks is None and self.vehicle_id is None
        all_present = self.started_tick is not None and self.duration_ticks is not None and bool(self.vehicle_id)
        if not (all_missing or all_present):
            raise ValueError("started_tick, duration_ticks and vehicle_id must be supplied together")
        return self


class ActivateResponse(BaseModel):
    hold_id: str
    request_id: str
    origin_fog: str
    proposer_fog: str
    pile_ids: list[str]
    status: Literal["ACTIVE"]


class CompleteRequest(BaseModel):
    hold_id: str = Field(min_length=1)


class CompleteResponse(BaseModel):
    hold_id: str
    request_id: str
    origin_fog: str
    proposer_fog: str
    pile_ids: list[str]
    status: Literal["COMPLETED"]


class LocalSessionRequest(BaseModel):
    vehicle_id: str = Field(min_length=1)
    station_id: str | None = None
    required_power_kw: float = Field(default=22.0, gt=0)
    duration_ticks: int = Field(default=2, ge=1, le=48)


class LocalSessionResponse(BaseModel):
    session_id: str
    vehicle_id: str
    fog_id: str
    station_id: str
    pile_id: str
    power_kw: float
    status: Literal["ACTIVE"]
    created_at: str
    duration_ticks: int | None = None
    due_tick: int | None = None


# ===========================================================================
# Scenario 5 — PREPARE / SETTLE Distributed Protocol Schemas
# ===========================================================================

class RoundPrepareRequest(BaseModel):
    tick: int = Field(ge=1, description="Round tick number")
    arrivals: list[LocalSessionRequest] = Field(default_factory=list)
    completions: list[str] = Field(default_factory=list, description="Hold IDs to complete")


class RoundPrepareResponse(BaseModel):
    fog_id: str
    tick: int
    status: Literal["PREPARED"]
    auto_completed: int = 0
    explicit_completed: int = 0
    completed_hold_ids: list[str] = Field(default_factory=list)
    served_sessions: list[LocalSessionResponse]
    unserved_vehicles: list[str]
    released_piles: list[str]
    available_piles: int
    occupied_piles: int


class RoundSettleRequest(BaseModel):
    tick: int = Field(ge=1, description="Round tick number")
    trigger_cooperation: bool = Field(default=True, description="Whether unserved vehicles initiate peer cooperation")
    remote_transfers: list[LocalSessionRequest] = Field(default_factory=list)


class RoundSettleResponse(BaseModel):
    fog_id: str
    tick: int
    status: Literal["SETTLED"]
    cooperation_results: list[dict] = Field(default_factory=list)
    accepted_transfers: list[LocalSessionResponse] = Field(default_factory=list)
    remaining_unserved: list[str] = Field(default_factory=list)
    available_piles: int
    occupied_piles: int


class RoundStatusResponse(BaseModel):
    fog_id: str
    tick: int
    phase: Literal["IDLE", "PREPARED", "SETTLED"]
    available_piles: int
    occupied_piles: int
    unserved_vehicles: list[str]

