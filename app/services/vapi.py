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


def _first_name(customer_name: str) -> str:
    return customer_name.strip().split()[0] if customer_name.strip() else "danke"


def _patient_speech_controls() -> dict[str, Any]:
    return {
        "startSpeakingPlan": {
            "waitSeconds": 0.45,
            "transcriptionEndpointingPlan": {
                "onPunctuationSeconds": 0.25,
                "onNoPunctuationSeconds": 1.1,
                "onNumberSeconds": 0.5,
            },
        },
        "stopSpeakingPlan": {
            "numWords": 2,
            "voiceSeconds": 0.25,
            "backoffSeconds": 0.8,
        },
    }


def _gemini_model(system_prompt: str) -> dict[str, Any]:
    return {
        "provider": "google",
        "model": settings.gemini_model,
        "temperature": 0.35,
        "maxTokens": 420,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            }
        ],
    }


def _server_config() -> dict[str, Any]:
    return {
        "url": f"{settings.public_base_url}/webhooks/vapi",
        "timeoutSeconds": 20,
    }


def _personalized_assistant_overrides(
    *,
    lead_id: str,
    customer_name: str,
    customer_email: str,
    variable_values: dict[str, Any],
    system_prompt: str,
) -> dict[str, Any]:
    first_name = _first_name(customer_name)
    return {
        **_patient_speech_controls(),
        "model": _gemini_model(system_prompt),
        "server": _server_config(),
        "serverMessages": ["status-update", "end-of-call-report", "transcript"],
        "analysisPlan": {
            "summaryPrompt": (
                "Summarize the solar phone call in the same language the customer used. "
                "Include motivation, objections, and clear next steps."
            ),
            "structuredDataPrompt": (
                "Extrahiere owner_status, roof_type, need, timeline, budget_range, "
                "decision_maker, objections, call_outcome und next_steps aus dem Telefonat."
            ),
            "structuredDataSchema": {
                "type": "object",
                "properties": {
                    "owner_status": {"type": "string"},
                    "roof_type": {"type": "string"},
                    "need": {"type": "string"},
                    "timeline": {"type": "string"},
                    "budget_range": {"type": "string"},
                    "decision_maker": {"type": "string"},
                    "objections": {"type": "array", "items": {"type": "string"}},
                    "call_outcome": {"type": "string"},
                    "next_steps": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "firstMessage": (
            f"Hallo {first_name}, hier ist SolarPingu. Danke fuer Ihre Anfrage. "
            "I will match your language during the call and quickly check the most "
            "important details about your solar request."
        ),
        "variableValues": {
            "lead_id": lead_id,
            "lead_name": customer_name,
            "lead_first_name": first_name,
            "lead_email": customer_email,
            "conversation_summary_email": settings.conversation_summary_email,
            "agent_behavior": (
                "Detect and mirror the customer's language. Address the customer "
                "naturally by name. Do not interrupt. Ask only one question at a "
                "time and summarize clear next steps at the end."
            ),
            **variable_values,
        },
    }


def build_customer_call_prompt(
    *,
    customer_name: str,
    offer: dict[str, Any],
    profitability: dict[str, Any],
    solar: dict[str, Any],
) -> str:
    price_range = offer.get("price_range") or {}
    reasons = profitability.get("reasons") or []
    disqualifiers = profitability.get("disqualifiers") or []
    return (
        "You are the SolarPingu phone advisor. Gemini is the reasoning engine "
        "for this call. Detect the customer's language and mirror it throughout "
        "the conversation. If the customer switches language, follow them.\n"
        f"Address the customer naturally by name: {customer_name}.\n"
        "Important: do not mention PDFs, internal demos, or technical systems. "
        "The customer should only experience a natural phone call.\n"
        "Goal: confirm the key details, answer objections, explain the assessment "
        "briefly, and agree on a clear next step.\n"
        "Ask only one thing at a time and wait for the answer.\n"
        "If the customer is interested, suggest a consultation or callback for "
        "final roof and consumption checks.\n\n"
        "Internal context:\n"
        f"- Entscheidung: {profitability.get('decision', 'unknown')}\n"
        f"- Score: {profitability.get('score', 'unknown')}/100\n"
        f"- Paket intern: {offer.get('package_name', 'Solarpaket')}\n"
        f"- Groesse intern: {offer.get('system_size_kwp', 'unknown')} kWp\n"
        f"- Preisrahmen intern: {price_range.get('min', 'unknown')} bis "
        f"{price_range.get('max', 'unknown')} {price_range.get('currency', 'EUR')}\n"
        f"- Gruende: {', '.join(str(item) for item in reasons) or 'keine'}\n"
        f"- Disqualifier: {', '.join(str(item) for item in disqualifiers) or 'keine'}\n"
        f"- Solardaten: {json.dumps(solar, ensure_ascii=False)[:1500]}\n"
    )


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
        f"- Address the customer by name: {customer_name}.",
        "- Speak naturally and briefly.",
        "- Wait patiently after customer answers; do not interrupt short pauses.",
        "- Pitch the offer in under 45 seconds.",
        "- Mention the price as a range, not a fixed final price.",
        "- If the customer asks for details, refer to the offer PDF context.",
        "- Ask one clear closing question: whether they want to book the next appointment.",
        "- At the end, summarize the collected information and clear next steps.",
        f"- Send or trigger the conversation summary for {settings.conversation_summary_email}.",
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
        "assistantOverrides": _personalized_assistant_overrides(
            lead_id=lead_id,
            customer_name=customer_name,
            customer_email=customer_email,
            variable_values={"call_plan": call_plan},
            system_prompt=(
                "You are a SolarPingu qualification advisor. Detect and mirror "
                "the customer's language. Keep the call concise, clarify missing "
                "qualification details, and summarize the next step."
            ),
        ),
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
        "assistantOverrides": _personalized_assistant_overrides(
            lead_id=lead_id,
            customer_name=customer_name,
            customer_email=customer_email,
            variable_values={
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
            },
            system_prompt=(
                "You are a SolarPingu offer advisor. Detect and mirror the "
                "customer's language, use the provided context naturally, and ask "
                "for the next appointment at the end."
            ),
        ),
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


async def create_customer_qualification_call(
    *,
    lead_id: str,
    customer_name: str,
    customer_number: str,
    customer_email: str,
    offer: dict[str, Any],
    profitability: dict[str, Any],
    solar: dict[str, Any],
) -> dict[str, Any]:
    if not settings.vapi_configured:
        return {
            "skipped": True,
            "reason": "Vapi is missing VAPI_API_KEY, VAPI_ASSISTANT_ID, or VAPI_PHONE_NUMBER_ID.",
        }

    system_prompt = build_customer_call_prompt(
        customer_name=customer_name,
        offer=offer,
        profitability=profitability,
        solar=solar,
    )
    body = {
        "assistantId": settings.vapi_assistant_id,
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": {
            "number": customer_number,
            "name": customer_name[:40],
            "email": customer_email,
        },
        "name": f"SolarPingu customer call - {lead_id}",
        "assistantOverrides": _personalized_assistant_overrides(
            lead_id=lead_id,
            customer_name=customer_name,
            customer_email=customer_email,
            variable_values={
                "offer": offer,
                "profitability": profitability,
                "solar": solar,
                "customer_call_goal": (
                    "Kunden anrufen, Eckdaten bestaetigen, Einwaende beantworten "
                    "und klare naechste Schritte vereinbaren."
                ),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            system_prompt=system_prompt,
        ),
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


def extract_call_text(payload: dict[str, Any]) -> tuple[str, str]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else call
    artifact = (
        message.get("artifact")
        if isinstance(message.get("artifact"), dict)
        else payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    )
    analysis = (
        message.get("analysis")
        if isinstance(message.get("analysis"), dict)
        else payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    )
    summary = (
        payload.get("summary")
        or message.get("summary")
        or analysis.get("summary")
        or call.get("summary")
        or ""
    )
    transcript = (
        payload.get("transcript")
        or message.get("transcript")
        or artifact.get("transcript")
        or call.get("transcript")
        or ""
    )
    if not transcript:
        transcript = _messages_to_transcript(
            artifact.get("messages")
            or message.get("messages")
            or payload.get("messages")
        )
    return str(summary or ""), str(transcript or "")


def extract_status(payload: dict[str, Any]) -> str:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else call
    return str(
        payload.get("status")
        or message.get("status")
        or call.get("status")
        or ""
    )


def _messages_to_transcript(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("speaker") or "message"
        content = item.get("message") or item.get("content") or item.get("text")
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)
