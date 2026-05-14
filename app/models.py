from __future__ import annotations

from datetime import datetime
from typing import Any

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
