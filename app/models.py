from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class Slot(BaseModel):
    value: str
    label: str
    start: datetime
    end: datetime


class LeadCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    email: EmailStr
    phone: str = Field(..., min_length=5, max_length=60)
    preferred_slot: datetime
    address: str = Field(default="", max_length=300)
    message: str = Field(default="", max_length=2000)


class Lead(BaseModel):
    lead_id: str
    created_at: datetime
    name: str
    email: EmailStr
    phone: str
    address: str
    message: str
    selected_slot_start: datetime
    selected_slot_end: datetime
    status: str
    calendar_event_id: str | None = None
    call_plan: dict[str, Any] | None = None


class RecordingCreate(BaseModel):
    lead_id: str = Field(..., min_length=1)
    recording_url: str = Field(..., min_length=8)


class RecordingAccepted(BaseModel):
    ok: bool
    lead_id: str
    speechmatics_job_id: str


OwnerStatus = Literal["owner", "renter", "unknown"]
RoofType = Literal["pitched", "flat", "unknown"]
Need = Literal["cost_savings", "independence", "both", "unknown"]
Timeline = Literal[
    "immediate",
    "within_3_months",
    "within_6_months",
    "within_12_months",
    "exploring",
]
BudgetRange = Literal[
    "under_10000",
    "10000-15000",
    "15000-20000",
    "20000-30000",
    "over_30000",
    "unknown",
]
PreferredContact = Literal["email", "phone", "both"]
Decision = Literal["PURSUE", "NURTURE", "REJECT"]
ResourceLevel = Literal["high_touch", "medium_touch", "low_touch"]
NextAction = Literal["send_booking_link", "request_missing_info", "polite_reject"]


class SolarLeadIntake(BaseModel):
    lead_id: str | None = Field(default=None, min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=160)
    email: EmailStr
    phone: str = Field(..., min_length=5, max_length=60)
    address: str = Field(..., min_length=8, max_length=300)
    owner_status: OwnerStatus
    roof_type: RoofType
    need: Need = "unknown"
    timeline: Timeline
    budget_range: BudgetRange
    decision_maker: str = Field(..., min_length=2, max_length=200)
    main_concern: str = Field(..., min_length=2, max_length=500)
    battery_interest: bool = False
    wallbox_interest: bool = False
    preferred_contact: PreferredContact = "email"


class ProfitabilityDecision(BaseModel):
    profitable: bool
    decision: Decision
    score: int = Field(..., ge=0, le=100)
    resource_level: ResourceLevel
    estimated_kwp: float = Field(..., ge=0)
    estimated_price_min: int = Field(..., ge=0)
    estimated_price_max: int = Field(..., ge=0)
    estimated_margin: int
    payback_years: float | None = None
    reasons: list[str]
    disqualifiers: list[str]
    next_action: NextAction


class OfferPriceRange(BaseModel):
    min: int
    max: int
    currency: Literal["EUR"] = "EUR"


class OfferDraft(BaseModel):
    offer_id: str
    lead_id: str
    package_name: str
    system_size_kwp: float
    includes_battery: bool
    price_range: OfferPriceRange
    value_pitch: list[str]
    assumptions: list[str]
    next_steps: list[str]


class HubHandoffPayload(BaseModel):
    source: Literal["solar-agent-fastapi"] = "solar-agent-fastapi"
    lead: SolarLeadIntake
    profitability: ProfitabilityDecision
    solar_enrichment: dict[str, Any]
    offer: OfferDraft
    demo_url: str
    created_at: str


class VoiceSessionCreate(BaseModel):
    lead_id: str = Field(..., min_length=1)
    prompt: str | None = Field(default=None, max_length=1000)


class VoiceAgentResult(BaseModel):
    lead_id: str
    intent: Literal["question", "objection", "ready_to_book", "opt_out", "closed"]
    response_text: str
    next_status: str
    staff_notification_required: bool = False
