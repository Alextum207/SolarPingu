from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from typing import Annotated, Any, Awaitable, Callable
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
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
    installers,
    offer,
    offer_pdf,
    profitability,
    solar_api,
    speechmatics,
    twilio_bridge,
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
    return index(request)


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
    return templates.TemplateResponse(
        request,
        "workflow.html",
        result,
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
    customer_call = await _start_customer_call(
        lead=lead,
        solar=solar,
        offer_data=offer_draft.model_dump(mode="json"),
        profitability_data=decision.model_dump(mode="json"),
    )
    final_status = customer_call.get("status") or status
    result = {
        "lead_id": lead_id,
        "status": final_status,
        "lead": lead.model_dump(mode="json"),
        "solar_enrichment": solar,
        "profitability": decision.model_dump(mode="json"),
        "offer": offer_draft.model_dump(mode="json"),
        "offer_pdf_url": pdf_url,
        "offer_pdf_local_url": f"/api/leads/{lead_id}/offer.pdf",
        "offer_pdf_path": str(pdf_path),
        "handoff": handoff.model_dump(mode="json"),
        "email": mail,
        "speechmatics_configured": bool(settings.speechmatics_api_key),
        "customer_call": customer_call,
    }
    await _emit_trace(
        trace_callback,
        scope="workflow",
        agent="Agent 1",
        step="Workflow abgeschlossen",
        status="DONE",
        message=f"Lead-Status: {final_status}.",
        lead_id=lead_id,
    )
    return result


async def _start_customer_call(
    *,
    lead: SolarLeadIntake,
    solar: dict[str, Any],
    offer_data: dict[str, Any],
    profitability_data: dict[str, Any],
) -> dict[str, Any]:
    lead_id = lead.lead_id or ""
    try:
        response = await twilio_bridge.create_customer_call(
            lead_id=lead_id,
            customer_number=lead.phone,
        )
    except Exception as exc:
        response = {"failed": True, "error": str(exc)}

    call_id = str(response.get("sid") or response.get("callSid") or response.get("id") or "")
    call_status = (
        "twilio_call_failed"
        if response.get("failed")
        else "twilio_call_skipped"
        if response.get("skipped")
        else "twilio_call_queued"
    )
    if call_id:
        db.add_vapi_event(
            lead_id=lead_id,
            call_id=call_id,
            event_type="twilio_customer_call_started",
            payload=response,
        )
    stored = db.get_agentic_lead(lead_id)
    preserved_status = str((stored or {}).get("status") or call_status)
    db.update_agentic_artifacts(
        lead_id,
        status=preserved_status,
        voice={
            "twilio_customer_call": response,
            "customer_call_status": call_status,
            "provider": "twilio",
        },
    )
    return {
        "status": call_status,
        "call_id": call_id,
        "response": response,
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
    lead_id = f"SL-{uuid4().hex[:10].upper()}"
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
        idempotency_key=f"booking-lead:{lead_id}:{start.isoformat()}",
    )
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
    if db.get_lead(lead_id) is None and db.get_agentic_lead(lead_id) is None:
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
    speechmatics_artifacts = speechmatics.callback_artifacts(payload)
    lead_id = speechmatics_artifacts["lead_id"]
    transcript = speechmatics_artifacts["transcript"]
    if not lead_id:
        raise HTTPException(status_code=400, detail="Missing lead_id in Speechmatics callback.")
    agentic = db.get_agentic_lead(lead_id)
    if db.get_lead(lead_id) is None and agentic is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    qualification = await gemini.extract_qualification(lead_id, transcript)
    qualification["conversation_turns"] = speechmatics_artifacts["conversation_turns"]
    qualification["low_confidence_terms"] = speechmatics_artifacts["low_confidence_terms"]
    if agentic is not None:
        lead = SolarLeadIntake.model_validate(agentic["intake"])
        prof = None
        if agentic.get("profitability"):
            from app.models import ProfitabilityDecision

            prof = ProfitabilityDecision.model_validate(agentic["profitability"])
        voice = await voice_agent.answer_from_transcript(lead, transcript, prof)
        summary_mail = email.send_conversation_summary(
            lead_id=lead_id,
            lead_name=lead.name,
            lead_email=str(lead.email),
            lead_phone=lead.phone,
            source="Speechmatics",
            transcript=transcript,
            conversation_turns=speechmatics_artifacts["conversation_turns"],
            qualification=qualification,
            voice_result=voice.model_dump(mode="json"),
            planning_context=_email_planning_context(agentic),
        )
        db.update_agentic_artifacts(
            lead_id,
            status=voice.next_status,
            voice={
                "transcript": transcript,
                "conversation_turns": speechmatics_artifacts["conversation_turns"],
                "low_confidence_terms": speechmatics_artifacts["low_confidence_terms"],
                "qualification": qualification,
                "voice_result": voice.model_dump(mode="json"),
                "summary_email": summary_mail,
            },
        )
        return {
            "ok": True,
            "lead_id": lead_id,
            "qualification": qualification,
            "voice": voice.model_dump(mode="json"),
            "summary_email": summary_mail,
        }

    db.complete_transcription(lead_id, transcript, qualification)
    lead_row = db.row_to_dict(db.get_lead(lead_id)) or {}
    summary_mail = email.send_conversation_summary(
        lead_id=lead_id,
        lead_name=str(lead_row.get("name") or lead_id),
        lead_email=str(lead_row.get("email") or ""),
        lead_phone=str(lead_row.get("phone") or ""),
        source="Speechmatics",
        transcript=transcript,
        conversation_turns=speechmatics_artifacts["conversation_turns"],
        qualification=qualification,
    )
    return {
        "ok": True,
        "lead_id": lead_id,
        "qualification": qualification,
        "summary_email": summary_mail,
    }


@app.get("/api/leads/{lead_id}/handoff")
def get_handoff(lead_id: str) -> dict[str, Any]:
    stored = db.get_agentic_lead(lead_id)
    if stored is None or not stored.get("handoff"):
        raise HTTPException(status_code=404, detail="Handoff not found.")
    return stored["handoff"]


def _email_planning_context(stored: dict[str, Any]) -> dict[str, Any]:
    try:
        slots = [
            slot.model_dump(mode="json")
            for slot in calendar.get_available_slots(max_slots=3)
        ]
    except Exception:
        slots = []
    installer_options = installers.installer_slot_options(max_slots_per_installer=3)
    return {
        "lead": stored.get("intake"),
        "solar": stored.get("solar"),
        "profitability": stored.get("profitability"),
        "offer": stored.get("offer"),
        "handoff": stored.get("handoff"),
        "available_slots": slots,
        "installers": installer_options,
        "call_recording": (stored.get("voice") or {}).get("twilio_recording"),
    }


@app.get("/installer/confirm/{lead_id}", response_class=HTMLResponse)
def confirm_installer_slot(
    lead_id: str,
    slot: str | None = None,
    installer_id: str | None = None,
) -> HTMLResponse:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    installer = installers.get_installer(installer_id)
    selected = _selected_installer_slot(slot, calendar_id=installer["calendar_id"])
    end = selected + timedelta(minutes=calendar.SLOT_MINUTES)
    voice = stored.get("voice") or {}
    existing_appointment = voice.get("installer_appointment") or {}
    if existing_appointment.get("confirmed"):
        return _installer_confirmation_page(
            lead=lead,
            lead_id=lead_id,
            installer_name=str(existing_appointment.get("installer_name") or installer["name"]),
            selected=datetime.fromisoformat(
                str(existing_appointment.get("start") or selected.isoformat())
            ).astimezone(settings.tz),
            address=lead.address,
            event_id=str(existing_appointment.get("calendar_event_id") or "already-confirmed"),
            reused=True,
        )
    booking = calendar.book_qualification_call(
        name=lead.name,
        email=str(lead.email),
        phone=lead.phone,
        address=lead.address,
        message="Finales Vor-Ort-Planungsgespraech mit Handwerker nach Lead-Call.",
        start=selected,
        end=end,
        calendar_id=installer["calendar_id"],
        idempotency_key=f"installer:{lead_id}:{installer['id']}:{selected.isoformat()}",
    )
    voice["installer_appointment"] = {
        "confirmed": True,
        "start": selected.isoformat(),
        "end": end.isoformat(),
        "installer_id": installer["id"],
        "installer_name": installer["name"],
        "calendar_id": installer["calendar_id"],
        "calendar_event_id": booking.event_id,
        "calendar_link": booking.html_link,
    }
    db.update_agentic_artifacts(lead_id, status="installer_appointment_confirmed", voice=voice)
    return _installer_confirmation_page(
        lead=lead,
        lead_id=lead_id,
        installer_name=installer["name"],
        selected=selected,
        address=lead.address,
        event_id=booking.event_id,
        reused=booking.reused,
    )


def _installer_confirmation_page(
    *,
    lead: SolarLeadIntake,
    lead_id: str,
    installer_name: str,
    selected: datetime,
    address: str,
    event_id: str,
    reused: bool,
) -> HTMLResponse:
    status_text = (
        "Dieser Lead hatte bereits einen bestaetigten Termin; es wurde kein zweiter Kalendertermin erstellt."
        if reused
        else "Der Vor-Ort-Planungstermin wurde geblockt."
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Termin bestaetigt</title></head>
<body style="font-family:Arial,sans-serif;background:#f4f7f2;color:#172018;margin:0;padding:32px;">
  <main style="max-width:680px;margin:0 auto;background:white;border:1px solid #dbe6d6;border-radius:8px;padding:24px;">
    <h1 style="margin-top:0;">Handwerker-Termin bestaetigt</h1>
    <p>{status_text}</p>
    <p><strong>Lead:</strong> {lead.name}</p>
    <p><strong>Handwerker:</strong> {installer_name}</p>
    <p><strong>Start:</strong> {selected.strftime('%d.%m.%Y %H:%M Uhr')}</p>
    <p><strong>Adresse:</strong> {address}</p>
    <p><strong>Kalender-Event:</strong> {event_id}</p>
    <p><a href="/demo/{lead_id}">Lead ansehen</a></p>
  </main>
</body>
</html>"""
    )


def _selected_installer_slot(slot: str | None, calendar_id: str | None = None) -> datetime:
    if slot:
        try:
            return datetime.fromisoformat(slot).astimezone(settings.tz)
        except ValueError:
            pass
    available = calendar.get_available_slots(max_slots=1, calendar_id=calendar_id)
    if available:
        return available[0].start
    return datetime.now(settings.tz) + timedelta(hours=2)


@app.get("/api/leads/{lead_id}/panel-plan.png")
def panel_plan_image(lead_id: str) -> StreamingResponse:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    image = _render_panel_plan_png(stored)
    return StreamingResponse(image, media_type="image/png")


def _render_panel_plan_png(stored: dict[str, Any]) -> BytesIO:
    from PIL import Image, ImageDraw, ImageFont
    import math

    lead = stored.get("intake") or {}
    offer = stored.get("offer") or {}
    profitability_data = stored.get("profitability") or {}
    solar = stored.get("solar") or {}
    potential = solar.get("solar_potential") or {}
    width, height = 1200, 1040
    image = Image.new("RGB", (width, height), "#f6f8f4")
    draw = ImageDraw.Draw(image)

    title_font = _pil_font("arialbd.ttf", 34)
    metric_label_font = _pil_font("arial.ttf", 22)
    metric_font = _pil_font("arialbd.ttf", 30)
    small_font = _pil_font("arial.ttf", 18)
    tag_font = _pil_font("arialbd.ttf", 16)

    system_size = float(
        offer.get("system_size_kwp")
        or potential.get("estimated_kwp")
        or profitability_data.get("estimated_kwp")
        or 0
    )
    yearly_kwh = int(potential.get("yearly_energy_kwh") or max(0, system_size * 930))
    modules = int(max(8, round((system_size or 8.5) / 0.4)))
    price = offer.get("price_range") or {}
    min_price = price.get("min") or profitability_data.get("estimated_price_min")
    max_price = price.get("max") or profitability_data.get("estimated_price_max")
    annual_savings = int(max(900, system_size * 205)) if system_size else "n/a"
    payback = profitability_data.get("payback_years") or (
        round(float(min_price) / max(900, float(annual_savings)), 1)
        if min_price and isinstance(annual_savings, int)
        else "n/a"
    )
    confidence = float(potential.get("confidence") or 0.73)
    ghosting_risk = max(3, min(88, int(round((1 - confidence) * 100))))

    draw.rounded_rectangle((42, 28, 1158, 76), radius=16, fill="#dceee2")
    draw.rounded_rectangle((42, 28, 42 + int(1116 * (1 - ghosting_risk / 100)), 76), radius=16, fill="#1ea366")
    draw.text((42, 100), "Ghosting-Risiko", fill="#526058", font=metric_label_font)
    draw.rounded_rectangle((42, 142, 432, 158), radius=8, fill="#dceee2")
    draw.rounded_rectangle((42, 142, 42 + int(390 * ghosting_risk / 100), 158), radius=8, fill="#1ea366")
    draw.text((1046, 95), f"{ghosting_risk} %", fill="#111815", font=metric_font)

    cards = [
        ("PV-GROESSE", f"{system_size:.1f} kWp" if system_size else "n/a"),
        ("JAHRES-KWH", f"{yearly_kwh:,} kWh".replace(",", ".")),
        ("MODULE", str(modules)),
        ("PREIS", _price_range_label(min_price, max_price)),
        ("ERSPARNIS / JAHR", f"{annual_savings:,} EUR".replace(",", ".") if isinstance(annual_savings, int) else "n/a"),
        ("AMORTISATION", f"{payback} J." if payback != "n/a" else "n/a"),
    ]
    for index, (label, value) in enumerate(cards):
        col = index % 3
        row = index // 3
        x = 72 + col * 360
        y = 205 + row * 115
        draw.rounded_rectangle((x, y, x + 300, y + 80), radius=4, fill="#f9fbf8", outline="#e1e7dd", width=1)
        draw.text((x + 16, y + 16), label, fill="#6b7770", font=metric_label_font)
        draw.text((x + 16, y + 43), value, fill="#111815", font=metric_font)

    map_box = (72, 455, 1128, 880)
    roof = _roof_plan_background(solar, map_box[2] - map_box[0], map_box[3] - map_box[1])
    image.paste(roof, map_box[:2])
    draw.rounded_rectangle(map_box, radius=8, outline="#d1d8cf", width=2)

    panel_layer = Image.new("RGBA", (440, 170), (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel_layer)
    cols = max(4, math.ceil(modules / 4))
    rows = max(2, math.ceil(modules / cols))
    panel_w = min(42, int(390 / cols))
    panel_h = min(34, int(135 / rows))
    gap = 4
    drawn = 0
    start_x = 18
    start_y = 18
    for row in range(rows):
        for col in range(cols):
            if drawn >= modules:
                break
            x = start_x + col * (panel_w + gap)
            y = start_y + row * (panel_h + gap)
            panel_draw.rectangle((x, y, x + panel_w, y + panel_h), fill="#071923", outline="#7dc7d8", width=1)
            panel_draw.line((x + panel_w // 2, y + 2, x + panel_w // 2, y + panel_h - 2), fill="#2d7180", width=1)
            panel_draw.line((x + 2, y + panel_h // 2, x + panel_w - 2, y + panel_h // 2), fill="#2d7180", width=1)
            drawn += 1
    panel_layer = panel_layer.rotate(-8, expand=True, resample=Image.Resampling.BICUBIC)
    px = map_box[0] + (map_box[2] - map_box[0] - panel_layer.width) // 2
    py = map_box[1] + (map_box[3] - map_box[1] - panel_layer.height) // 2
    image.paste(panel_layer, (px, py), panel_layer)

    draw.rounded_rectangle((90, 472, 310, 502), radius=8, fill="#eef4ec", outline="#c8d4c4", width=1)
    draw.text((104, 479), f"Potential preview {system_size:.1f} kWp", fill="#314036", font=tag_font)
    draw.text((84, 856), "Google", fill="#f5f5f5", font=title_font, stroke_width=2, stroke_fill="#2b2f2b")

    source = solar.get("source") or "deterministic_fallback"
    address = str(lead.get("address") or "Adresse unbekannt")
    draw.text((72, 922), f"Adresse: {address[:76]}", fill="#344039", font=small_font)
    draw.text(
        (72, 958),
        f"Geocode: GOOGLE_GEOCODING_API      Solar: GOOGLE_SOLAR_API      Roof: {source.upper()}",
        fill="#526058",
        font=small_font,
    )
    draw.text(
        (72, 994),
        "Layout: GOOGLE_SOLAR_PANEL_HEURISTIC      Finale Belegung wird beim Vor-Ort-Termin geprueft.",
        fill="#4c5b50",
        font=small_font,
    )
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def _pil_font(name: str, size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _price_range_label(min_price: Any, max_price: Any) -> str:
    if min_price and max_price:
        return f"{int(float(min_price)):,} - {int(float(max_price)):,} EUR".replace(",", ".")
    return "n/a"


def _roof_plan_background(solar: dict[str, Any], width: int, height: int) -> Any:
    from PIL import Image, ImageDraw

    coordinates = solar.get("coordinates") or {}
    if coordinates and settings.google_solar_api_key:
        try:
            static_url = "https://maps.googleapis.com/maps/api/staticmap"
            with httpx.Client(timeout=4) as client:
                response = client.get(
                    static_url,
                    params={
                        "center": f"{coordinates['lat']},{coordinates['lng']}",
                        "zoom": 20,
                        "size": f"{width}x{height}",
                        "maptype": "satellite",
                        "key": settings.google_solar_api_key,
                    },
                )
                response.raise_for_status()
            return Image.open(BytesIO(response.content)).convert("RGB").resize((width, height))
        except Exception:
            pass

    roof = Image.new("RGB", (width, height), "#52665c")
    draw = ImageDraw.Draw(roof)
    draw.rectangle((0, 0, width, height), fill="#3f554a")
    draw.polygon([(0, 0), (260, 0), (170, height), (0, height)], fill="#8da06e")
    draw.polygon([(780, 0), (width, 0), (width, height), (900, height)], fill="#394c42")
    draw.polygon([(270, 20), (770, 0), (850, height), (190, height)], fill="#56636b")
    draw.polygon([(360, 40), (665, 28), (712, height - 35), (310, height - 10)], fill="#68737a")
    draw.line((505, 30, 515, height - 20), fill="#2f3942", width=7)
    for x in range(70, width, 130):
        draw.rectangle((x, 30, x + 55, 110), fill="#75808a")
    for x in range(120, width, 180):
        draw.rectangle((x, height - 95, x + 80, height - 35), fill="#2f3934")
    for y in range(70, height, 100):
        draw.line((0, y, width, y + 30), fill="#48574e", width=2)
    return roof


def _font_exists(name: str) -> bool:
    try:
        from PIL import ImageFont

        ImageFont.truetype(name, 12)
        return True
    except OSError:
        return False


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


@app.api_route("/webhooks/twilio/voice/{lead_id}", methods=["GET", "POST"])
async def twilio_voice_twiml(lead_id: str) -> Response:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = SolarLeadIntake.model_validate(stored["intake"])
    twiml = twilio_bridge.build_conversation_relay_twiml(lead_id, lead)
    return Response(content=twiml, media_type="application/xml")


@app.post("/webhooks/twilio/status/{lead_id}")
async def twilio_status_callback(lead_id: str, request: Request) -> dict[str, Any]:
    form = await request.form()
    payload = twilio_bridge.status_payload(dict(form))
    call_sid = payload.get("CallSid") or payload.get("CallSid".lower()) or ""
    call_status = payload.get("CallStatus") or payload.get("CallStatus".lower()) or "unknown"
    db.add_vapi_event(
        lead_id=lead_id,
        call_id=call_sid,
        event_type=f"twilio_status_{call_status}",
        payload=payload,
    )
    stored = db.get_agentic_lead(lead_id)
    if stored is not None:
        voice = stored.get("voice") or {}
        voice["twilio_status"] = payload
        db.update_agentic_artifacts(
            lead_id,
            status=f"twilio_call_{call_status}",
            voice=voice,
        )
    return {"ok": True, "lead_id": lead_id, "call_sid": call_sid, "status": call_status}


@app.post("/webhooks/twilio/recording/{lead_id}")
async def twilio_recording_callback(lead_id: str, request: Request) -> dict[str, Any]:
    form = await request.form()
    payload = twilio_bridge.status_payload(dict(form))
    call_sid = payload.get("CallSid") or ""
    recording_sid = payload.get("RecordingSid") or ""
    recording_url = payload.get("RecordingUrl") or ""
    recording_status = payload.get("RecordingStatus") or "unknown"
    duration = payload.get("RecordingDuration")
    download_url = (
        f"{settings.public_base_url}/api/leads/{lead_id}/call-audio"
        f"?recording_sid={recording_sid}"
        if recording_sid
        else ""
    )

    db.add_vapi_event(
        lead_id=lead_id,
        call_id=call_sid,
        event_type=f"twilio_recording_{recording_status}",
        payload=payload,
    )
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        return {"ok": False, "lead_id": lead_id, "status": "lead_not_found"}

    voice = stored.get("voice") or {}
    recording = {
        "call_sid": call_sid,
        "recording_sid": recording_sid,
        "recording_url": recording_url,
        "recording_status": recording_status,
        "duration": duration,
        "download_url": download_url,
        "payload": payload,
    }
    voice["twilio_recording"] = recording

    mail = None
    already_sent_for = (voice.get("twilio_recording_email") or {}).get("recording_sid")
    if recording_status == "completed" and recording_url and already_sent_for != recording_sid:
        mail = await _send_call_recording_email(stored, recording)
        voice["twilio_recording_email"] = {
            "recording_sid": recording_sid,
            "status": mail.get("status") if mail else "not_sent",
            "sent": bool(mail and mail.get("sent")),
        }

    db.update_agentic_artifacts(
        lead_id,
        status=str(stored.get("status") or "twilio_recording_available"),
        voice=voice,
    )
    return {
        "ok": True,
        "lead_id": lead_id,
        "recording_sid": recording_sid,
        "status": recording_status,
        "summary_email": mail,
    }


@app.get("/api/leads/{lead_id}/call-audio")
async def download_call_audio(lead_id: str, recording_sid: str | None = None) -> Response:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Lead not found.")
    recording = (stored.get("voice") or {}).get("twilio_recording") or {}
    if recording_sid and recording.get("recording_sid") != recording_sid:
        raise HTTPException(status_code=404, detail="Recording not found.")
    recording_url = recording.get("recording_url")
    if not recording_url:
        raise HTTPException(status_code=404, detail="Recording URL not available yet.")
    audio = await twilio_bridge.download_recording_audio(recording_url, lead_id=lead_id)
    headers = {
        "Content-Disposition": f'attachment; filename="{audio["filename"]}"',
        "Cache-Control": "private, max-age=300",
    }
    return Response(
        content=audio["content"],
        media_type=audio["content_type"],
        headers=headers,
    )


async def _send_call_recording_email(
    stored: dict[str, Any],
    recording: dict[str, Any],
) -> dict[str, Any]:
    lead = SolarLeadIntake.model_validate(stored["intake"])
    attachment = None
    recording_url = str(recording.get("recording_url") or "")
    if recording_url:
        try:
            audio = await twilio_bridge.download_recording_audio(
                recording_url,
                lead_id=lead.lead_id or "",
            )
            max_bytes = settings.call_audio_attachment_max_mb * 1024 * 1024
            if len(audio["content"]) <= max_bytes:
                attachment = {
                    "filename": audio["filename"],
                    "content": audio["content"],
                    "content_type": audio["content_type"],
                }
        except Exception as exc:
            recording["attachment_error"] = str(exc)
    return email.send_call_recording_email(
        lead_id=lead.lead_id or "",
        lead_name=lead.name,
        lead_email=str(lead.email),
        lead_phone=lead.phone,
        recording=recording,
        attachment=attachment,
    )


@app.api_route("/webhooks/twilio/relay-ended/{lead_id}", methods=["GET", "POST"])
async def twilio_relay_ended(lead_id: str, request: Request) -> Response:
    form = await request.form() if request.method == "POST" else request.query_params
    payload = twilio_bridge.status_payload(dict(form))
    db.add_vapi_event(
        lead_id=lead_id,
        call_id=payload.get("CallSid") or "",
        event_type="twilio_relay_ended",
        payload=payload,
    )
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup /></Response>',
        media_type="application/xml",
    )


@app.websocket("/ws/twilio/conversation/{lead_id}")
async def twilio_conversation_ws(websocket: WebSocket, lead_id: str) -> None:
    await twilio_bridge.handle_conversation_ws(websocket, lead_id)


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
    voice_payload = voice.model_dump(mode="json")
    summary_mail = None
    if voice.intent in {"closed", "ready_to_book"}:
        summary_mail = email.send_conversation_summary(
            lead_id=payload.lead_id,
            lead_name=lead.name,
            lead_email=str(lead.email),
            lead_phone=lead.phone,
            source="Browser Voice Demo",
            transcript=transcript,
            voice_result=voice_payload,
            planning_context=_email_planning_context(stored),
        )
        voice_payload["summary_email"] = summary_mail
    db.update_agentic_artifacts(
        payload.lead_id,
        status=voice.next_status,
        voice={"transcript": transcript, "voice_result": voice_payload},
    )
    return voice_payload


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
        {
            "lead_id": lead_id,
            "record": stored,
            "lead": stored.get("intake"),
            "speechmatics_configured": bool(settings.speechmatics_api_key),
        },
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
    if lead_id and event_type == "status-update":
        call_status = vapi.extract_status(payload)
        if call_status:
            agentic = db.get_agentic_lead(lead_id)
            if agentic is not None:
                voice = agentic.get("voice") or {}
                voice["vapi_status"] = call_status
                db.update_agentic_artifacts(
                    lead_id,
                    status=f"call_{call_status}",
                    voice=voice,
                )
    if lead_id and event_type in {"end-of-call-report", "call-ended", "ended"}:
        db.update_status(lead_id, "call_completed")
        summary, transcript = vapi.extract_call_text(payload)
        if summary or transcript:
            contact = _conversation_summary_contact(lead_id)
            summary_mail = email.send_conversation_summary(
                lead_id=lead_id,
                lead_name=contact["name"],
                lead_email=contact["email"],
                lead_phone=contact["phone"],
                source="Vapi",
                transcript=transcript,
                call_summary=summary,
                planning_context=_email_planning_context(db.get_agentic_lead(lead_id) or {}),
            )
            agentic = db.get_agentic_lead(lead_id)
            if agentic is not None:
                voice = agentic.get("voice") or {}
                voice["vapi_summary"] = {
                    "summary": summary,
                    "transcript": transcript,
                    "summary_email": summary_mail,
                }
                db.update_agentic_artifacts(lead_id, status="call_completed", voice=voice)
    return {"ok": True, "lead_id": lead_id, "call_id": call_id, "event_type": event_type}


def _conversation_summary_contact(lead_id: str) -> dict[str, str]:
    agentic = db.get_agentic_lead(lead_id)
    if agentic is not None:
        lead = SolarLeadIntake.model_validate(agentic["intake"])
        return {
            "name": lead.name,
            "email": str(lead.email),
            "phone": lead.phone,
        }
    lead = db.row_to_dict(db.get_lead(lead_id)) or {}
    return {
        "name": str(lead.get("name") or lead_id),
        "email": str(lead.get("email") or ""),
        "phone": str(lead.get("phone") or ""),
    }


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
