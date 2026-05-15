from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any, Awaitable, Callable
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from app import db
from app.config import settings
from app.models import (
    LeadCreate,
    OperatorProjectCreate,
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
    offer_pdf,
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://127.0.0.1:5176",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://localhost:5176",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory="templates")

TraceCallback = Callable[[dict[str, Any]], Awaitable[None]]


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


@app.get("/intake")
def intake_page(request: Request):
    if request.query_params.get("legacy") == "1":
        return index(request)
    return RedirectResponse(settings.frontend_url, status_code=302)


@app.get("/api/slots", response_model=list[Slot])
def api_slots() -> list[Slot]:
    return calendar.get_available_slots()


@app.post("/api/leads")
async def create_lead_json(payload: LeadCreate) -> dict[str, Any]:
    return await _create_lead(payload)


@app.get("/api/leads")
def list_leads(
    limit: int = 100,
    source: str = "all",
    status: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    normalized = _list_normalized_leads(
        limit=limit,
        source=source,
        status=status,
        project_id=project_id,
    )
    return {
        "count": len(normalized),
        "leads": normalized,
        "filters": {
            "source": source,
            "status": status,
            "project_id": project_id,
            "limit": limit,
        },
    }


@app.get("/api/projects")
def list_projects(limit: int = 100) -> dict[str, Any]:
    projects = [_normalize_project(project) for project in db.list_operator_projects(limit=limit)]
    return {"count": len(projects), "projects": projects}


@app.post("/api/projects")
def create_project(payload: OperatorProjectCreate) -> dict[str, Any]:
    city = payload.city.strip()
    name = (payload.name or "").strip() or f"{city} Projekt"
    project_id = f"PRJ-{uuid4().hex[:10].upper()}"
    project = db.create_operator_project(project_id=project_id, name=name, city=city)
    return {"ok": True, "project": _normalize_project(project)}


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
    return RedirectResponse(
        f"{settings.frontend_url}?leadId={result['lead_id']}&autoRun=1",
        status_code=303,
    )


async def _store_intake(payload: SolarLeadIntake) -> dict[str, Any]:
    lead_id = payload.lead_id or f"SL-{uuid4().hex[:10].upper()}"
    intake = payload.model_copy(update={"lead_id": lead_id})
    if intake.project_id and db.get_operator_project(intake.project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    db.upsert_agentic_lead(
        lead_id,
        intake.model_dump(mode="json"),
        project_id=intake.project_id,
    )
    if intake.project_id:
        db.touch_operator_project(intake.project_id)
    return {"ok": True, "lead_id": lead_id, "intake": intake.model_dump(mode="json")}


@app.post("/api/workflows/{lead_id}/run")
async def run_agentic_workflow(lead_id: str) -> dict[str, Any]:
    return await _run_agentic_workflow(lead_id)


@app.get("/api/workflows/{lead_id}/stream", include_in_schema=False)
async def stream_agentic_workflow(lead_id: str) -> StreamingResponse:
    return StreamingResponse(
        _workflow_event_stream(lead_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_agentic_workflow(
    lead_id: str,
    trace_callback: TraceCallback | None = None,
) -> dict[str, Any]:
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Agent 1",
        step="Workflow starten",
        status="RUNNING",
        message="Lead wird geladen und fuer den Agentic Workflow vorbereitet.",
        lead_id=lead_id,
    )
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        await _emit_trace(
            trace_callback,
            scope="workflow",
            agent="Agent 1",
            step="Lead laden",
            status="FAILED",
            message="Lead wurde nicht gefunden.",
            lead_id=lead_id,
        )
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Agent 1",
        step="Lead geladen",
        status="DONE",
        message=f"{lead.name} ist bereit fuer die Bewertung.",
        detail=lead.address,
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Solar Enrichment",
        step="Solarpotential pruefen",
        status="RUNNING",
        message="Google Solar oder der Demo-Fallback ermittelt Dach- und Ertragssignale.",
        lead_id=lead_id,
    )
    solar = await solar_api.enrich_solar_potential(lead)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Solar Enrichment",
        step="Solarpotential fertig",
        status="WARN" if solar.get("warning") else "DONE",
        message=solar.get("warning") or "Solarpotential wurde angereichert.",
        detail=str(solar.get("source") or ""),
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Profitability Agent",
        step="Profitabilitaet bewerten",
        status="RUNNING",
        message="Budget, Timing, Eigentuemerstatus, Dachsignal und Add-ons werden gewichtet.",
        lead_id=lead_id,
    )
    decision = profitability.evaluate_profitability(lead, solar)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Profitability Agent",
        step="Entscheidung fertig",
        status="DONE",
        message=f"{decision.decision} mit Score {decision.score}/100.",
        detail=", ".join(decision.reasons[:3]),
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Offer Agent",
        step="Angebot erstellen",
        status="RUNNING",
        message="Angebot, Preisrahmen und naechste Schritte werden formuliert.",
        lead_id=lead_id,
    )
    offer_draft = await offer.create_offer(lead, decision, solar)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Offer Agent",
        step="Angebot fertig",
        status="DONE",
        message=offer_draft.package_name,
        detail=f"{offer_draft.system_size_kwp:.1f} kWp",
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="PDF Agent",
        step="PDF generieren",
        status="RUNNING",
        message="Das Angebot wird als PDF-Artefakt erzeugt.",
        lead_id=lead_id,
    )
    pdf_path = offer_pdf.generate_offer_pdf(lead, decision, offer_draft, solar)
    pdf_url = offer_pdf.offer_pdf_url(lead_id)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="PDF Agent",
        step="PDF fertig",
        status="DONE",
        message="Angebots-PDF ist verfuegbar.",
        detail=str(pdf_path),
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Handoff Agent",
        step="Handoff vorbereiten",
        status="RUNNING",
        message="Payload fuer Hub, Demo und Sales-Uebergabe wird zusammengestellt.",
        lead_id=lead_id,
    )
    handoff = hub.create_handoff(lead, decision, solar, offer_draft, pdf_url)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Handoff Agent",
        step="Handoff fertig",
        status="DONE",
        message="Handoff-Payload ist gespeichert.",
        lead_id=lead_id,
    )
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Email Agent",
        step="Naechste Aktion ausloesen",
        status="RUNNING",
        message="Booking-, Nurture- oder Reject-Kommunikation wird vorbereitet.",
        lead_id=lead_id,
    )
    mail = email.send_decision_email(lead, decision)
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Email Agent",
        step="Kommunikation fertig",
        status="WARN" if mail.get("status") == "demo_logged" else "DONE",
        message=(
            "SMTP fehlt; E-Mail wurde im Demo-Log gespeichert."
            if mail.get("status") == "demo_logged"
            else "E-Mail wurde versendet."
        ),
        detail=str(mail.get("status") or ""),
        lead_id=lead_id,
    )
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
    result = {
        "lead_id": lead_id,
        "status": status,
        "lead": lead.model_dump(mode="json"),
        "solar_enrichment": solar,
        "profitability": decision.model_dump(mode="json"),
        "offer": offer_draft.model_dump(mode="json"),
        "offer_pdf_url": pdf_url,
        "offer_pdf_local_url": f"/api/leads/{lead_id}/offer.pdf",
        "offer_pdf_path": str(pdf_path),
        "handoff": handoff.model_dump(mode="json"),
        "email": mail,
    }
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Agent 1",
        step="Workflow abgeschlossen",
        status="DONE",
        message=f"Lead-Status: {status}.",
        lead_id=lead_id,
    )
    return result


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
    db.upsert_agentic_lead(
        intake.lead_id or f"L-{uuid4().hex[:6].upper()}",
        intake.model_dump(mode="json"),
        status="evaluated_from_hub",
    )
    pdf_path = offer_pdf.generate_offer_pdf(intake, decision, offer_draft, solar)
    pdf_url = offer_pdf.offer_pdf_url(intake.lead_id or "")
    handoff = hub.create_handoff(intake, decision, solar, offer_draft, pdf_url)
    db.update_agentic_artifacts(
        intake.lead_id or "",
        status="hub_offer_ready",
        solar=solar,
        profitability=decision.model_dump(mode="json"),
        offer=offer_draft.model_dump(mode="json"),
        handoff=handoff.model_dump(mode="json"),
    )
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
        "offerPdfUrl": pdf_url,
        "offerPdfPath": str(pdf_path),
        "handoffUrl": f"{settings.public_base_url}/api/leads/{intake.lead_id}/handoff",
        "demoUrl": f"{settings.public_base_url}/demo/{intake.lead_id}",
    }


@app.post("/api/finder/run")
async def run_finder_from_agent1(payload: dict[str, Any]) -> dict[str, Any]:
    city = str(payload.get("city") or "").strip()
    if not city:
        raise HTTPException(status_code=400, detail="city is required")
    project_id = str(payload.get("project_id") or payload.get("projectId") or "").strip() or None
    if project_id and db.get_operator_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project_id:
        db.touch_operator_project(project_id, last_run=True)

    timeout = httpx.Timeout(120.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.agent2_base_url}/finder/run",
                json={"city": city},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Finder Agent 2 unavailable: {exc.__class__.__name__}",
        ) from exc
    if project_id:
        _assign_finder_response_to_project(data, project_id)
    return data


@app.get("/api/finder/stream", include_in_schema=False)
async def stream_finder_from_agent1(
    city: str,
    project_id: str | None = None,
) -> StreamingResponse:
    if not city.strip():
        raise HTTPException(status_code=400, detail="city is required")
    if project_id and db.get_operator_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project_id:
        db.touch_operator_project(project_id, last_run=True)
    return StreamingResponse(
        _finder_proxy_event_stream(city.strip(), project_id=project_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/finder/leads")
def list_finder_leads(limit: int = 100) -> dict[str, Any]:
    records = db.list_agentic_leads(
        status="finder_lead_received",
        limit=limit,
    )
    leads = [
        {
            "lead_id": record["lead_id"],
            "created_at": record["created_at"],
            "status": record["status"],
            "intake": record.get("intake"),
            "solar": record.get("solar"),
            "handoffUrl": f"{settings.public_base_url}/api/leads/{record['lead_id']}/handoff",
            "leadUrl": f"{settings.public_base_url}/api/leads/{record['lead_id']}",
        }
        for record in records
    ]
    return {"count": len(leads), "leads": leads}


@app.post("/api/finder/leads")
async def receive_finder_lead(payload: dict[str, Any]) -> dict[str, Any]:
    lead_id = str(payload.get("leadId") or payload.get("lead_id") or f"FINDER-{uuid4().hex[:10].upper()}")
    project_id = str(payload.get("projectId") or payload.get("project_id") or "").strip() or None
    if project_id and db.get_operator_project(project_id) is None:
        project_id = None
    business_name = str(payload.get("businessName") or "Finder Lead")
    address = str(payload.get("address") or "Unknown address")
    phone = str(payload.get("phone") or "+490000000")
    if len(phone) < 5:
        phone = "+490000000"
    safe_email_id = "".join(char.lower() for char in lead_id if char.isalnum())[:40] or "finder"
    solar = payload.get("solar") if isinstance(payload.get("solar"), dict) else {}
    vision = payload.get("vision") if isinstance(payload.get("vision"), dict) else {}
    website = str(payload.get("website") or "")
    maps_url = str(payload.get("googleMapsUrl") or "")

    intake = SolarLeadIntake(
        lead_id=lead_id,
        project_id=project_id,
        name=business_name,
        email=f"finder+{safe_email_id}@solarpingu.de",
        phone=phone,
        address=address,
        owner_status="unknown",
        roof_type="flat" if "flat" in str(vision.get("roofType", "")).lower() else "unknown",
        need="cost_savings",
        timeline="exploring",
        budget_range="unknown",
        decision_maker="Finder public business data",
        main_concern=(
            f"Finder lead from {payload.get('source', 'unknown source')}. "
            f"Category: {payload.get('category', 'unknown')}. "
            f"Website: {website or 'n/a'}. Maps: {maps_url or 'n/a'}."
        )[:500],
        battery_interest=False,
        wallbox_interest=False,
        preferred_contact="email",
    )
    db.upsert_agentic_lead(
        lead_id,
        intake.model_dump(mode="json"),
        status="finder_lead_received",
        project_id=project_id,
    )
    if project_id:
        db.touch_operator_project(project_id)
    db.update_agentic_artifacts(
        lead_id,
        status="finder_lead_received",
        solar={
            "source": payload.get("source"),
            "category": payload.get("category"),
            "rating": payload.get("rating"),
            "website": website,
            "googleMapsUrl": maps_url,
            "finderSolar": solar,
            "finderVision": vision,
            "publicInfoOnly": payload.get("publicInfoOnly", True),
            "visionWarning": payload.get("visionWarning"),
        },
    )
    return {
        "ok": True,
        "lead_id": lead_id,
        "status": "finder_lead_received",
        "handoffUrl": f"{settings.public_base_url}/api/leads/{lead_id}/handoff",
        "leadUrl": f"{settings.public_base_url}/api/leads/{lead_id}",
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


@app.get("/api/leads/{lead_id}/offer.pdf")
def get_offer_pdf(lead_id: str) -> FileResponse:
    path = offer_pdf.offer_pdf_path(lead_id)
    if not path.exists():
        stored = db.get_agentic_lead(lead_id)
        if stored is None or not stored.get("offer") or not stored.get("profitability"):
            raise HTTPException(status_code=404, detail="Offer PDF not found.")
        from app.models import OfferDraft, ProfitabilityDecision

        offer_pdf.generate_offer_pdf(
            SolarLeadIntake.model_validate(stored["intake"]),
            ProfitabilityDecision.model_validate(stored["profitability"]),
            OfferDraft.model_validate(stored["offer"]),
            stored.get("solar") or {},
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"solar_offer_{lead_id}.pdf",
    )


@app.post("/api/leads/{lead_id}/vapi-offer-call")
async def vapi_offer_call(
    request: Request,
    lead_id: str,
):
    content_type = request.headers.get("content-type", "")
    accept = request.headers.get("accept", "")
    wants_json = "application/json" in accept or "application/json" in content_type
    phone_number = None
    if "application/json" in content_type:
        body = await request.json()
        phone_number = body.get("phone_number") or body.get("phoneNumber")
    else:
        form = await request.form()
        phone_number = form.get("phone_number")

    stored = db.get_agentic_lead(lead_id)
    if stored is None or not stored.get("offer") or not stored.get("profitability"):
        raise HTTPException(status_code=404, detail="Lead offer not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    number = phone_number or lead.phone
    pdf_url = offer_pdf.offer_pdf_url(lead_id)
    try:
        response = await vapi.create_offer_demo_call(
            lead_id=lead_id,
            customer_name=lead.name,
            customer_number=number,
            customer_email=str(lead.email),
            offer=stored["offer"],
            profitability=stored["profitability"],
            offer_pdf_url=pdf_url,
        )
        call_id = response.get("id") or response.get("callId") or ""
        if call_id:
            db.add_vapi_event(
                lead_id=lead_id,
                call_id=str(call_id),
                event_type="offer_demo_call_started",
                payload=response,
            )
        status = "failed" if response.get("failed") else "queued" if not response.get("skipped") else "skipped"
        error = None
    except Exception as exc:
        response = {}
        call_id = ""
        status = "failed"
        error = str(exc)
    if response.get("failed") and not error:
        error = f"Vapi {response.get('status_code')}: {response.get('error')}"
    if wants_json:
        return JSONResponse(
            {
                "ok": status not in {"failed", "skipped"},
                "lead_id": lead_id,
                "phone_number": number,
                "status": status,
                "call_id": call_id,
                "error": error,
                "hint": response.get("hint"),
                "response": response,
            }
        )
    return templates.TemplateResponse(
        request,
        "call_result.html",
        {
            "lead_id": lead_id,
            "phone_number": number,
            "status": status,
            "call_id": call_id,
            "error": error,
            "response": response,
        },
        status_code=200 if status != "failed" else 400,
    )


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


def _list_normalized_leads(
    *,
    limit: int,
    source: str,
    status: str | None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    source = source.lower()
    if source not in {"all", "website", "b2b_finder", "booking"}:
        raise HTTPException(
            status_code=400,
            detail="source must be one of all, website, b2b_finder, booking",
        )

    records: list[dict[str, Any]] = []
    if source in {"all", "website", "b2b_finder"}:
        for record in db.list_agentic_leads(limit=safe_limit, project_id=project_id):
            normalized = _normalize_agentic_lead(record)
            if source != "all" and normalized["source"] != source:
                continue
            records.append(normalized)
    if source in {"all", "booking"} and not project_id:
        records.extend(
            _normalize_booking_lead(record)
            for record in db.list_booking_leads(limit=safe_limit)
        )

    if status:
        records = [record for record in records if record["status"] == status]

    records.sort(key=lambda item: item["updated_at"] or item["created_at"], reverse=True)
    return records[:safe_limit]


def _normalize_agentic_lead(record: dict[str, Any]) -> dict[str, Any]:
    intake = record.get("intake") or {}
    solar = record.get("solar") or {}
    profitability = record.get("profitability") or {}
    offer_payload = record.get("offer") or {}
    lead_id = record["lead_id"]
    is_finder = (
        record.get("status") == "finder_lead_received"
        or lead_id.startswith("FINDER-")
        or "finderSolar" in solar
    )
    finder_solar = solar.get("finderSolar") if isinstance(solar.get("finderSolar"), dict) else {}
    decision = profitability.get("decision") or finder_solar.get("decision")
    score = profitability.get("score")
    if score is None and finder_solar.get("profitabilityScore") is not None:
        score = round(float(finder_solar["profitabilityScore"]) * 100)
    links = _lead_links(lead_id, has_offer=bool(offer_payload or profitability))
    project = db.get_operator_project(record["project_id"]) if record.get("project_id") else None
    return {
        "lead_id": lead_id,
        "project_id": record.get("project_id"),
        "project_name": project.get("name") if project else None,
        "project_city": project.get("city") if project else None,
        "source": "b2b_finder" if is_finder else "website",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at") or record.get("created_at"),
        "status": record.get("status"),
        "name": intake.get("name"),
        "email": intake.get("email"),
        "phone": intake.get("phone"),
        "address": intake.get("address"),
        "owner_status": intake.get("owner_status"),
        "roof_type": intake.get("roof_type"),
        "timeline": intake.get("timeline"),
        "budget_range": intake.get("budget_range"),
        "decision": decision,
        "score": score,
        "estimated_kwp": profitability.get("estimated_kwp") or finder_solar.get("estimatedKwPeak"),
        "category": solar.get("category"),
        "rating": solar.get("rating"),
        "website": solar.get("website"),
        "googleMapsUrl": solar.get("googleMapsUrl"),
        "has_offer": bool(offer_payload or profitability),
        **links,
    }


def _normalize_booking_lead(record: dict[str, Any]) -> dict[str, Any]:
    lead_id = record["lead_id"]
    return {
        "lead_id": lead_id,
        "project_id": None,
        "project_name": None,
        "project_city": None,
        "source": "booking",
        "created_at": record.get("created_at"),
        "updated_at": record.get("created_at"),
        "status": record.get("status"),
        "name": record.get("name"),
        "email": record.get("email"),
        "phone": record.get("phone"),
        "address": record.get("address"),
        "owner_status": None,
        "roof_type": None,
        "timeline": record.get("selected_slot_start"),
        "budget_range": None,
        "decision": None,
        "score": None,
        "estimated_kwp": None,
        "category": "booking",
        "rating": None,
        "website": None,
        "googleMapsUrl": None,
        "has_offer": False,
        **_lead_links(lead_id, has_offer=False),
    }


def _lead_links(lead_id: str, *, has_offer: bool) -> dict[str, str | None]:
    return {
        "leadUrl": f"{settings.public_base_url}/api/leads/{lead_id}",
        "demoUrl": f"{settings.public_base_url}/demo/{lead_id}",
        "handoffUrl": f"{settings.public_base_url}/api/leads/{lead_id}/handoff",
        "offerPdfUrl": (
            f"{settings.public_base_url}/api/leads/{lead_id}/offer.pdf"
            if has_offer
            else None
        ),
    }


def _normalize_project(project: dict[str, Any]) -> dict[str, Any]:
    project_id = project["project_id"]
    project_leads = [
        _normalize_agentic_lead(record)
        for record in db.list_agentic_leads(limit=500, project_id=project_id)
    ]
    return {
        "project_id": project_id,
        "name": project["name"],
        "city": project["city"],
        "status": project["status"],
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "last_run_at": project.get("last_run_at"),
        "lead_count": len(project_leads),
        "b2b_count": len([lead for lead in project_leads if lead["source"] == "b2b_finder"]),
        "website_count": len([lead for lead in project_leads if lead["source"] == "website"]),
        "ready_count": len([lead for lead in project_leads if lead["status"] in {"booking_link_sent", "hub_offer_ready", "call_scheduled", "objection_handled"}]),
        "open_count": len([lead for lead in project_leads if lead["status"] in {"intake_received", "finder_lead_received", "evaluated_from_hub", "nurture_info_requested"}]),
    }


def _assign_finder_response_to_project(response: dict[str, Any], project_id: str) -> list[str]:
    assigned: list[str] = []
    if db.get_operator_project(project_id) is None:
        return assigned
    for lead in response.get("leads") or []:
        if not isinstance(lead, dict):
            continue
        lead_id = str(lead.get("leadId") or lead.get("lead_id") or "").strip()
        if not lead_id:
            continue
        db.assign_agentic_lead_project(lead_id, project_id)
        assigned.append(lead_id)
    if assigned:
        db.touch_operator_project(project_id)
    return assigned


async def _workflow_event_stream(lead_id: str):
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def emit_trace(event: dict[str, Any]) -> None:
        await queue.put({"type": "trace", "event": event})

    async def run_job() -> None:
        try:
            response = await _run_agentic_workflow(lead_id, trace_callback=emit_trace)
            await queue.put({"type": "final", "response": response})
        except HTTPException as exc:
            await queue.put(
                {
                    "type": "fail",
                    "event": _trace_event(
                        scope="workflow",
                        agent="Agent 1",
                        step="Workflow fehlgeschlagen",
                        status="FAILED",
                        message=str(exc.detail),
                        lead_id=lead_id,
                    ),
                    "message": str(exc.detail),
                }
            )
        except Exception as exc:
            await queue.put(
                {
                    "type": "fail",
                    "event": _trace_event(
                        scope="workflow",
                        agent="Agent 1",
                        step="Workflow fehlgeschlagen",
                        status="FAILED",
                        message=f"{exc.__class__.__name__}: {str(exc)[:300]}",
                        lead_id=lead_id,
                    ),
                    "message": f"{exc.__class__.__name__}: {str(exc)[:300]}",
                }
            )
        finally:
            await queue.put({"type": "done"})

    task = asyncio.create_task(run_job())
    try:
        while True:
            item = await queue.get()
            event_type = str(item.pop("type"))
            yield _sse_encode(event_type, item)
            if event_type in {"done", "fail"}:
                break
    finally:
        if not task.done():
            task.cancel()


async def _finder_proxy_event_stream(city: str, *, project_id: str | None = None):
    start_event = _trace_event(
        scope="finder",
        agent="Agent 2",
        step="Finder verbinden",
        status="RUNNING",
        message="Agent 1 oeffnet den Live-Stream zum B2B Finder.",
        detail=city,
        project_id=project_id,
    )
    yield _sse_encode("trace", {"event": start_event})
    try:
        timeout = httpx.Timeout(180.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET",
                f"{settings.agent2_base_url}/finder/stream",
                params={"city": city},
            ) as response:
                response.raise_for_status()
                event_type = "message"
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        if data_lines:
                            payload = json.loads("\n".join(data_lines))
                            for outgoing_type, outgoing_payload in _normalize_finder_sse(
                                event_type,
                                payload,
                                project_id=project_id,
                            ):
                                yield _sse_encode(outgoing_type, outgoing_payload)
                        event_type = "message"
                        data_lines = []
                    elif line.startswith("event:"):
                        event_type = line.removeprefix("event:").strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.removeprefix("data:").strip())
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {str(exc)[:300]}"
        yield _sse_encode(
            "fail",
            {
                "event": _trace_event(
                    scope="finder",
                    agent="Agent 2",
                    step="Finder fehlgeschlagen",
                    status="FAILED",
                    message=message,
                    detail=city,
                    project_id=project_id,
                ),
                "message": message,
            },
        )
    finally:
        yield _sse_encode("done", {})


def _normalize_finder_sse(
    event_type: str,
    payload: dict[str, Any],
    *,
    project_id: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    if event_type == "trace":
        raw = payload.get("event") or {}
        return [
            (
                "trace",
                {
                    "event": _trace_event(
                        scope="finder",
                        agent="Agent 2",
                        step=str(raw.get("step") or "Finder"),
                        status=str(raw.get("status") or "RUNNING"),
                        message=str(raw.get("thought") or raw.get("step") or "Finder arbeitet."),
                        detail=raw.get("detail"),
                        business_name=raw.get("businessName"),
                        address=raw.get("address"),
                        project_id=project_id,
                    )
                },
            )
        ]
    if event_type == "final":
        response = payload.get("response") or {}
        assigned_leads = _assign_finder_response_to_project(response, project_id) if project_id else []
        event = _trace_event(
            scope="finder",
            agent="Agent 2",
            step="Finder abgeschlossen",
            status="DONE",
            message=(
                f"{response.get('qualifiedCount', 0)} qualifiziert, "
                f"{response.get('sentToAgent1Count', 0)} an Agent 1 gesendet."
            ),
            detail=response.get("runId") if not assigned_leads else f"{response.get('runId')} | Projekt: {len(assigned_leads)} Leads",
            project_id=project_id,
        )
        return [
            ("trace", {"event": event}),
            ("final", {"response": {**response, "projectId": project_id, "assignedLeadIds": assigned_leads}}),
        ]
    if event_type == "fail":
        message = str(payload.get("message") or "Finder fehlgeschlagen.")
        return [
            (
                "fail",
                {
                    "event": _trace_event(
                        scope="finder",
                        agent="Agent 2",
                        step="Finder fehlgeschlagen",
                        status="FAILED",
                        message=message,
                        project_id=project_id,
                    ),
                    "message": message,
                },
            )
        ]
    return []


async def _emit_trace(
    trace_callback: TraceCallback | None,
    **event_kwargs: Any,
) -> dict[str, Any]:
    event = _trace_event(**event_kwargs)
    if trace_callback is not None:
        await trace_callback(event)
    return event


def _trace_event(
    *,
    scope: str,
    agent: str,
    step: str,
    status: str,
    message: str,
    detail: Any | None = None,
    lead_id: str | None = None,
    project_id: str | None = None,
    business_name: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    return {
        "ts": db.now_iso(),
        "scope": scope,
        "agent": agent,
        "step": step,
        "status": status,
        "message": message,
        "detail": detail,
        "lead_id": lead_id,
        "project_id": project_id,
        "business_name": business_name,
        "address": address,
    }


def _sse_encode(event_type: str, payload: dict[str, Any]) -> str:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


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
