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
from app.models import (
    LeadCreate,
    RecordingAccepted,
    Slot,
    SolarLeadIntake,
    VoiceSessionCreate,
)
from app.services import (
    calendar,
    email,
    gemini,
    hub,
    offer,
    profitability,
    solar_api,
    speechmatics,
    vapi,
    voice_agent,
)


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "public_base_url": settings.public_base_url,
        },
    )


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request) -> HTMLResponse:
    return index(request)


@app.get("/api/slots", response_model=list[Slot])
def api_slots() -> list[Slot]:
    return calendar.get_available_slots()


@app.post("/api/leads")
async def create_lead_json(payload: LeadCreate) -> dict[str, Any]:
    return await _create_lead(payload)


@app.post("/api/intake")
async def intake_json(payload: SolarLeadIntake) -> dict[str, Any]:
    return await _store_intake(payload)


@app.post("/intake", response_class=HTMLResponse)
async def intake_form(
    request: Request,
    name: Annotated[str, Form()],
    email_address: Annotated[str, Form()],
    phone: Annotated[str, Form()],
    address: Annotated[str, Form()],
    owner_status: Annotated[str, Form()],
    roof_type: Annotated[str, Form()],
    need: Annotated[str, Form()],
    timeline: Annotated[str, Form()],
    budget_range: Annotated[str, Form()],
    decision_maker: Annotated[str, Form()],
    main_concern: Annotated[str, Form()],
    preferred_contact: Annotated[str, Form()],
    battery_interest: Annotated[bool | None, Form()] = False,
    wallbox_interest: Annotated[bool | None, Form()] = False,
) -> HTMLResponse:
    try:
        intake = SolarLeadIntake(
            name=name,
            email=email_address,
            phone=phone,
            address=address,
            owner_status=owner_status,
            roof_type=roof_type,
            need=need,
            timeline=timeline,
            budget_range=budget_range,
            decision_maker=decision_maker,
            main_concern=main_concern,
            battery_interest=bool(battery_interest),
            wallbox_interest=bool(wallbox_interest),
            preferred_contact=preferred_contact,
        )
        stored = await _store_intake(intake)
        result = await _run_agentic_workflow(stored["lead_id"])
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": str(exc),
                "public_base_url": settings.public_base_url,
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "workflow.html",
        result,
    )


async def _store_intake(payload: SolarLeadIntake) -> dict[str, Any]:
    lead_id = payload.lead_id or f"SL-{uuid4().hex[:10].upper()}"
    intake = payload.model_copy(update={"lead_id": lead_id})
    db.upsert_agentic_lead(lead_id, intake.model_dump(mode="json"))
    return {"ok": True, "lead_id": lead_id, "intake": intake.model_dump(mode="json")}


@app.post("/api/workflows/{lead_id}/run")
async def run_agentic_workflow(lead_id: str) -> dict[str, Any]:
    return await _run_agentic_workflow(lead_id)


async def _run_agentic_workflow(lead_id: str) -> dict[str, Any]:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    solar = await solar_api.enrich_solar_potential(lead)
    decision = profitability.evaluate_profitability(lead, solar)
    offer_draft = await offer.create_offer(lead, decision, solar)
    handoff = hub.create_handoff(lead, decision, solar, offer_draft)
    mail = email.send_decision_email(lead, decision)
    status = {
        "PURSUE": "booking_link_sent",
        "NURTURE": "nurture_info_requested",
        "REJECT": "closed_not_a_fit",
    }[decision.decision]
    db.update_agentic_artifacts(
        lead_id,
        status=status,
        solar=solar,
        profitability=decision.model_dump(mode="json"),
        offer=offer_draft.model_dump(mode="json"),
        handoff=handoff.model_dump(mode="json"),
    )
    return {
        "lead_id": lead_id,
        "status": status,
        "lead": lead.model_dump(mode="json"),
        "solar_enrichment": solar,
        "profitability": decision.model_dump(mode="json"),
        "offer": offer_draft.model_dump(mode="json"),
        "handoff": handoff.model_dump(mode="json"),
        "email": mail,
    }


@app.post("/agent2/evaluate")
async def agent2_evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    intake = SolarLeadIntake(
        lead_id=payload.get("leadId") or payload.get("lead_id") or f"L-{uuid4().hex[:6].upper()}",
        name=payload.get("name") or "Demo Lead",
        email=payload.get("email") or "demo@example.com",
        phone=payload.get("phone") or "+490000000",
        address=payload.get("address") or "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
        owner_status=payload.get("ownerStatus") or payload.get("owner_status") or "unknown",
        roof_type=payload.get("roofType") or payload.get("roof_type") or "unknown",
        need=payload.get("need") or "both",
        timeline=payload.get("installationTimeline") or payload.get("timeline") or "exploring",
        budget_range=payload.get("budgetRange") or payload.get("budget_range") or "unknown",
        decision_maker=payload.get("decisionMaker") or payload.get("decision_maker") or "unknown",
        main_concern=(
            ", ".join(payload.get("objections") or [])
            if isinstance(payload.get("objections"), list)
            else payload.get("mainConcern") or payload.get("main_concern") or "unknown"
        ),
        battery_interest=bool(payload.get("batteryInterest") or payload.get("battery_interest")),
        wallbox_interest=bool(payload.get("wallboxInterest") or payload.get("wallbox_interest")),
        preferred_contact=payload.get("preferredContact") or "email",
    )
    solar = await solar_api.enrich_solar_potential(intake)
    decision = profitability.evaluate_profitability(intake, solar)
    offer_draft = await offer.create_offer(intake, decision, solar)
    return {
        "decision": decision.decision,
        "resourceLevel": decision.resource_level,
        "nextAction": decision.next_action,
        "assignedRep": "Inside Sales",
        "reasoning": "Profitabilität wurde aus Eigentümerstatus, Budget, Timing, Dach-/Solarpotenzial und Add-ons berechnet.",
        "leadFitScore": round(decision.score / 100, 2),
        "profitabilityScore": round(decision.score / 100, 2),
        "ghostingRiskScore": 0.28 if decision.decision == "PURSUE" else 0.55,
        "estimatedKwPeak": decision.estimated_kwp,
        "yearlyEnergyKwh": solar.get("solar_potential", {}).get("yearly_energy_kwh"),
        "estimatedPriceMin": decision.estimated_price_min,
        "estimatedPriceMax": decision.estimated_price_max,
        "annualSavingsEstimate": int(decision.estimated_kwp * 220),
        "paybackYears": decision.payback_years,
        "panelCount": int(max(8, decision.estimated_kwp / 0.42)),
        "panelLayoutConfidence": solar.get("solar_potential", {}).get("confidence", 0.62),
        "solarSource": solar.get("source"),
        "fallbackWarning": solar.get("warning"),
        "reasons": decision.reasons,
        "disqualifiers": decision.disqualifiers,
        "offer": offer_draft.model_dump(mode="json"),
        "handoffUrl": f"{settings.public_base_url}/api/leads/{intake.lead_id}/handoff",
        "demoUrl": f"{settings.public_base_url}/demo/{intake.lead_id}",
    }


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
    vapi_call = await vapi.create_outbound_call(
        lead_id=lead_id,
        customer_name=payload.name,
        customer_number=payload.phone,
        customer_email=str(payload.email),
        schedule_at=start,
        call_plan=call_plan,
    )
    if not vapi_call.get("skipped"):
        call_id = str(vapi_call.get("id") or vapi_call.get("callId") or "")
        if call_id:
            db.update_vapi_call(lead_id, call_id)
            lead_data["status"] = "call_scheduled"
            lead_data["vapi_call_id"] = call_id

    return {"lead": lead_data, "call_plan": call_plan, "vapi_call": vapi_call}


@app.post("/api/leads/{lead_id}/call")
async def start_vapi_call(lead_id: str) -> dict[str, Any]:
    lead = db.row_to_dict(db.get_lead(lead_id))
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if not vapi.is_configured():
        raise HTTPException(
            status_code=400,
            detail="Vapi is missing VAPI_API_KEY, VAPI_ASSISTANT_ID, or VAPI_PHONE_NUMBER_ID.",
        )
    call_plan = lead.get("call_plan") or {}
    schedule_at = LeadCreate.model_validate(
        {
            "name": lead["name"],
            "email": lead["email"],
            "phone": lead["phone"],
            "address": lead["address"],
            "message": lead["message"],
            "preferred_slot": lead["selected_slot_start"],
        }
    ).preferred_slot.astimezone(settings.tz)
    response = await vapi.create_outbound_call(
        lead_id=lead_id,
        customer_name=lead["name"],
        customer_number=lead["phone"],
        customer_email=lead["email"],
        schedule_at=schedule_at,
        call_plan=call_plan,
    )
    call_id = str(response.get("id") or response.get("callId") or "")
    if call_id:
        db.update_vapi_call(lead_id, call_id)
    return {"ok": True, "lead_id": lead_id, "vapi_call": response}


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
    agentic = db.get_agentic_lead(lead_id)
    if db.get_lead(lead_id) is None and agentic is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    qualification = await gemini.extract_qualification(lead_id, transcript)
    if agentic is not None:
        lead = SolarLeadIntake.model_validate(agentic["intake"])
        prof = None
        if agentic.get("profitability"):
            from app.models import ProfitabilityDecision

            prof = ProfitabilityDecision.model_validate(agentic["profitability"])
        voice = await voice_agent.answer_from_transcript(lead, transcript, prof)
        db.update_agentic_artifacts(
            lead_id,
            status=voice.next_status,
            voice={
                "transcript": transcript,
                "qualification": qualification,
                "voice_result": voice.model_dump(mode="json"),
            },
        )
        return {
            "ok": True,
            "lead_id": lead_id,
            "qualification": qualification,
            "voice": voice.model_dump(mode="json"),
        }

    db.complete_transcription(lead_id, transcript, qualification)
    return {"ok": True, "lead_id": lead_id, "qualification": qualification}


@app.get("/api/leads/{lead_id}/handoff")
def get_handoff(lead_id: str) -> dict[str, Any]:
    stored = db.get_agentic_lead(lead_id)
    if stored is None or not stored.get("handoff"):
        raise HTTPException(status_code=404, detail="Handoff not found.")
    return stored["handoff"]


@app.get("/api/leads/{lead_id}/offer")
def get_offer(lead_id: str) -> dict[str, Any]:
    stored = db.get_agentic_lead(lead_id)
    if stored is None or not stored.get("offer"):
        raise HTTPException(status_code=404, detail="Offer not found.")
    return stored["offer"]


@app.post("/api/voice/session")
async def voice_session(payload: VoiceSessionCreate) -> dict[str, Any]:
    stored = db.get_agentic_lead(payload.lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    prof = None
    if stored.get("profitability"):
        from app.models import ProfitabilityDecision

        prof = ProfitabilityDecision.model_validate(stored["profitability"])
    transcript = payload.prompt or "Bitte pitchen Sie mir das Solar-Projekt kurz."
    voice = await voice_agent.answer_from_transcript(lead, transcript, prof)
    db.update_agentic_artifacts(
        payload.lead_id,
        status=voice.next_status,
        voice={"transcript": transcript, "voice_result": voice.model_dump(mode="json")},
    )
    return voice.model_dump(mode="json")


@app.get("/book/{lead_id}", response_class=HTMLResponse)
def book_page(request: Request, lead_id: str) -> HTMLResponse:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return templates.TemplateResponse(
        request,
        "book.html",
        {"lead_id": lead_id, "lead": stored.get("intake"), "public_base_url": settings.public_base_url},
    )


@app.get("/demo/{lead_id}", response_class=HTMLResponse)
def demo_page(request: Request, lead_id: str) -> HTMLResponse:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return templates.TemplateResponse(
        request,
        "demo.html",
        {"lead_id": lead_id, "record": stored},
    )


@app.post("/webhooks/vapi")
async def vapi_callback(payload: dict[str, Any]) -> dict[str, Any]:
    lead_id, call_id, event_type = vapi.extract_event(payload)
    if not lead_id and call_id:
        lead = db.row_to_dict(db.get_lead_by_vapi_call_id(call_id))
        lead_id = lead["lead_id"] if lead else None

    db.add_vapi_event(
        lead_id=lead_id,
        call_id=call_id,
        event_type=event_type,
        payload=payload,
    )
    if lead_id and event_type in {"end-of-call-report", "call-ended", "ended"}:
        db.update_status(lead_id, "call_completed")
    return {"ok": True, "lead_id": lead_id, "call_id": call_id, "event_type": event_type}


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str) -> dict[str, Any]:
    agentic = db.get_agentic_lead(lead_id)
    if agentic is not None:
        return agentic
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
