from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.config import settings
from app.models import LeadCreate, RecordingAccepted, Slot
from app.services import calendar, gemini, speechmatics


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="SolarPingu Agent 1", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    slots = calendar.get_available_slots()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "slots": slots,
            "calendar_configured": bool(settings.google_application_credentials),
        },
    )


@app.get("/api/slots", response_model=list[Slot])
def api_slots() -> list[Slot]:
    return calendar.get_available_slots()


@app.post("/api/leads")
async def create_lead_json(payload: LeadCreate) -> dict[str, Any]:
    return await _create_lead(payload)


@app.post("/leads", response_class=HTMLResponse)
async def create_lead_form(
    request: Request,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    phone: Annotated[str, Form()],
    preferred_slot: Annotated[str, Form()],
    address: Annotated[str, Form()] = "",
    message: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        payload = LeadCreate(
            name=name,
            email=email,
            phone=phone,
            address=address,
            message=message,
            preferred_slot=preferred_slot,
        )
        result = await _create_lead(payload)
    except Exception as exc:
        slots = calendar.get_available_slots()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "slots": slots,
                "error": str(exc),
                "calendar_configured": bool(settings.google_application_credentials),
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "success.html",
        {
            "lead": result["lead"],
            "call_plan": result["call_plan"],
        },
    )


async def _create_lead(payload: LeadCreate) -> dict[str, Any]:
    start = payload.preferred_slot.astimezone(settings.tz)
    end = start + timedelta(minutes=calendar.SLOT_MINUTES)
    if not calendar.is_slot_available(start):
        raise HTTPException(status_code=409, detail="Selected slot is no longer available.")

    booking = calendar.book_qualification_call(
        name=payload.name,
        email=str(payload.email),
        phone=payload.phone,
        address=payload.address,
        message=payload.message,
        start=start,
        end=end,
    )
    lead_id = f"SL-{uuid4().hex[:10].upper()}"
    lead_data = {
        "lead_id": lead_id,
        "created_at": db.now_iso(),
        "name": payload.name,
        "email": str(payload.email),
        "phone": payload.phone,
        "address": payload.address,
        "message": payload.message,
        "selected_slot_start": start.isoformat(),
        "selected_slot_end": end.isoformat(),
        "status": "scheduled",
        "calendar_event_id": booking.event_id,
        "call_plan_json": "{}",
    }
    db.create_lead(lead_data)
    call_plan = await gemini.create_call_plan(lead_data)
    db.update_call_plan(lead_id, call_plan)
    lead_data["call_plan"] = call_plan
    return {"lead": lead_data, "call_plan": call_plan}


@app.post("/api/recordings", response_model=RecordingAccepted)
async def create_recording_job(
    lead_id: Annotated[str, Form()],
    recording_url: Annotated[str | None, Form()] = None,
    audio_file: Annotated[UploadFile | None, File()] = None,
) -> RecordingAccepted:
    if db.get_lead(lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if not recording_url and audio_file is None:
        raise HTTPException(status_code=400, detail="Provide recording_url or audio_file.")

    if audio_file is not None:
        response = await speechmatics.submit_audio_file(
            lead_id,
            audio_file.filename or "recording.wav",
            await audio_file.read(),
        )
        stored_url = None
    else:
        assert recording_url is not None
        response = await speechmatics.submit_recording_url(lead_id, recording_url)
        stored_url = recording_url

    job_id = str(response.get("id") or response.get("job_id") or uuid4().hex)
    db.add_transcription_job(job_id, lead_id, stored_url, response)
    return RecordingAccepted(ok=True, lead_id=lead_id, speechmatics_job_id=job_id)


@app.post("/webhooks/speechmatics")
async def speechmatics_callback(payload: dict[str, Any]) -> dict[str, Any]:
    lead_id, transcript = speechmatics.transcript_from_callback(payload)
    if not lead_id:
        raise HTTPException(status_code=400, detail="Missing lead_id in Speechmatics callback.")
    if db.get_lead(lead_id) is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    qualification = await gemini.extract_qualification(lead_id, transcript)
    db.complete_transcription(lead_id, transcript, qualification)
    return {"ok": True, "lead_id": lead_id, "qualification": qualification}


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str) -> dict[str, Any]:
    lead = db.row_to_dict(db.get_lead(lead_id))
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return lead


@app.get("/api/debug/example-callback")
def example_callback_payload(lead_id: str) -> dict[str, Any]:
    return {
        "job": {"tracking": {"lead_id": lead_id}},
        "results": [
            {"alternatives": [{"content": "Hallo"}]},
            {"alternatives": [{"content": "ich"}]},
            {"alternatives": [{"content": "besitze"}]},
            {"alternatives": [{"content": "das"}]},
            {"alternatives": [{"content": "Haus"}]},
        ],
    }
