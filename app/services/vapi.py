from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import httpx

from app.config import settings


def _international_free_number_hint(customer_number: str) -> str | None:
    if customer_number.strip().startswith("+49"):
        return (
            "Vapi free numbers cannot make international calls. "
            "Import a Twilio, Telnyx, Vonage, or SIP number for German test calls, "
            "or test with a US destination number."
        )
    return None


def is_configured() -> bool:
    return settings.vapi_configured


def build_offer_context_markdown(
    *,
    lead_id: str,
    customer_name: str,
    customer_number: str,
    customer_email: str,
    offer: dict[str, Any],
    profitability: dict[str, Any],
    offer_pdf_url: str,
) -> str:
    price_range = offer.get("price_range") or {}
    value_pitch = offer.get("value_pitch") or []
    assumptions = offer.get("assumptions") or []
    next_steps = offer.get("next_steps") or []
    reasons = profitability.get("reasons") or []
    disqualifiers = profitability.get("disqualifiers") or []
    lines = [
        f"# SolarPingu Vapi Offer Context - {lead_id}",
        "",
        "## Customer",
        f"- Name: {customer_name}",
        f"- Phone: {customer_number}",
        f"- Email: {customer_email}",
        f"- Lead ID: {lead_id}",
        "",
        "## Offer",
        f"- Package: {offer.get('package_name', 'Solar package')}",
        f"- System size: {offer.get('system_size_kwp', 'unknown')} kWp",
        f"- Battery included: {offer.get('includes_battery', False)}",
        (
            "- Price range: "
            f"{price_range.get('min', 'unknown')} - {price_range.get('max', 'unknown')} "
            f"{price_range.get('currency', 'EUR')}"
        ),
        f"- Offer PDF: {offer_pdf_url}",
        "",
        "## Profitability Decision",
        f"- Decision: {profitability.get('decision', 'unknown')}",
        f"- Score: {profitability.get('score', 'unknown')}/100",
        f"- Resource level: {profitability.get('resource_level', 'unknown')}",
        f"- Estimated margin: {profitability.get('estimated_margin', 'unknown')}",
        f"- Payback years: {profitability.get('payback_years', 'unknown')}",
        "",
        "## Reasons",
        *(f"- {item}" for item in reasons),
        "",
        "## Disqualifiers",
        *(f"- {item}" for item in disqualifiers or ["None"]),
        "",
        "## Value Pitch",
        *(f"- {item}" for item in value_pitch),
        "",
        "## Assumptions",
        *(f"- {item}" for item in assumptions),
        "",
        "## Next Steps",
        *(f"- {item}" for item in next_steps),
        "",
        "## Voice Agent Instructions",
        "- Speak naturally and briefly.",
        "- Pitch the offer in under 45 seconds.",
        "- Mention the price as a range, not a fixed final price.",
        "- If the customer asks for details, refer to the offer PDF context.",
        "- Ask one clear closing question: whether they want to book the next appointment.",
        "",
        "## Raw Structured Data",
        "```json",
        json.dumps({"offer": offer, "profitability": profitability}, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(str(line) for line in lines)


async def upload_context_file(
    *,
    lead_id: str,
    markdown: str,
) -> dict[str, Any]:
    if not settings.vapi_api_key:
        return {"skipped": True, "reason": "VAPI_API_KEY is missing."}

    filename = f"SolarPingu_{lead_id}_offer_context.md"
    headers = {"Authorization": f"Bearer {settings.vapi_api_key}"}
    files = {
        "file": (
            filename,
            markdown.encode("utf-8"),
            "text/markdown",
        )
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings.vapi_file_url, headers=headers, files=files)
            if response.status_code >= 400:
                return {
                    "failed": True,
                    "status_code": response.status_code,
                    "error": response.text,
                }
    except httpx.HTTPError as exc:
        return {"failed": True, "error": str(exc)}
    return response.json()


async def create_outbound_call(
    *,
    lead_id: str,
    customer_name: str,
    customer_number: str,
    customer_email: str,
    schedule_at: datetime,
    call_plan: dict[str, Any],
) -> dict[str, Any]:
    if not settings.vapi_configured:
        return {
            "skipped": True,
            "reason": "Vapi is missing VAPI_API_KEY, VAPI_ASSISTANT_ID, or VAPI_PHONE_NUMBER_ID.",
        }

    body = {
        "assistantId": settings.vapi_assistant_id,
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": {
            "number": customer_number,
            "name": customer_name[:40],
            "email": customer_email,
        },
        "schedulePlan": {
            "earliestAt": schedule_at.isoformat(),
        },
        "name": f"SolarPingu Agent 1 - {lead_id}",
        "assistantOverrides": {
            "variableValues": {
                "lead_id": lead_id,
                "lead_name": customer_name,
                "lead_email": customer_email,
                "call_plan": call_plan,
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.vapi_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(settings.vapi_call_url, json=body, headers=headers)
        if response.status_code >= 400:
            return {
                "failed": True,
                "status_code": response.status_code,
                "error": response.text,
                "hint": _international_free_number_hint(customer_number),
            }
    return response.json()


async def create_offer_demo_call(
    *,
    lead_id: str,
    customer_name: str,
    customer_number: str,
    customer_email: str,
    offer: dict[str, Any],
    profitability: dict[str, Any],
    offer_pdf_url: str,
) -> dict[str, Any]:
    if not settings.vapi_configured:
        return {
            "skipped": True,
            "reason": "Vapi is missing VAPI_API_KEY, VAPI_ASSISTANT_ID, or VAPI_PHONE_NUMBER_ID.",
        }

    context_markdown = build_offer_context_markdown(
        lead_id=lead_id,
        customer_name=customer_name,
        customer_number=customer_number,
        customer_email=customer_email,
        offer=offer,
        profitability=profitability,
        offer_pdf_url=offer_pdf_url,
    )
    uploaded_file = await upload_context_file(lead_id=lead_id, markdown=context_markdown)
    file_id = uploaded_file.get("id") if not uploaded_file.get("failed") else None
    file_url = uploaded_file.get("url") if not uploaded_file.get("failed") else None

    body = {
        "assistantId": settings.vapi_assistant_id,
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": {
            "number": customer_number,
            "name": customer_name[:40],
            "email": customer_email,
        },
        "name": f"SolarPingu offer demo - {lead_id}",
        "assistantOverrides": {
            "variableValues": {
                "lead_id": lead_id,
                "lead_name": customer_name,
                "lead_email": customer_email,
                "offer": offer,
                "profitability": profitability,
                "offer_pdf_url": offer_pdf_url,
                "vapi_file_id": file_id,
                "vapi_file_url": file_url,
                "offer_context_markdown": context_markdown,
                "demo_goal": (
                    "Pitch the solar offer briefly, answer objections, and ask "
                    "whether the customer wants to book the next appointment."
                ),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.vapi_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(settings.vapi_call_url, json=body, headers=headers)
        if response.status_code >= 400:
            return {
                "failed": True,
                "status_code": response.status_code,
                "error": response.text,
                "hint": _international_free_number_hint(customer_number),
                "uploaded_context_file": uploaded_file,
            }
    data = response.json()
    data["uploaded_context_file"] = uploaded_file
    return data


def extract_event(payload: dict[str, Any]) -> tuple[str | None, str | None, str]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else call

    call_id = (
        payload.get("callId")
        or payload.get("call_id")
        or call.get("id")
        or message.get("callId")
    )
    event_type = (
        payload.get("type")
        or message.get("type")
        or payload.get("event")
        or "unknown"
    )

    variable_values = (
        call.get("assistantOverrides", {}).get("variableValues", {})
        if isinstance(call.get("assistantOverrides"), dict)
        else {}
    )
    lead_id = (
        payload.get("lead_id")
        or message.get("lead_id")
        or variable_values.get("lead_id")
    )
    return lead_id, call_id, str(event_type)
