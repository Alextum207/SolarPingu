from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings


def is_configured() -> bool:
    return settings.vapi_configured


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
        response.raise_for_status()
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
        response.raise_for_status()
    return response.json()


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
