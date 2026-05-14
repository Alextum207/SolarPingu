from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings


AGENT_SYSTEM_PROMPT = """You are the Qualification Agent for Solar Lead OS.

GOAL
Qualify warm solar leads quickly and politely. Collect the essential information needed for a serious proposal.
Do not sell hard. Do not sound robotic. Do not waste the lead's time.

STYLE
- Calm, professional, and direct.
- Use short, simple sentences.
- Ask one question at a time.
- Keep the conversation natural.
- Avoid jargon.
- Match the lead's language.

CORE QUESTIONS
1. Ownership: Do you own the property yourself?
2. Roof: What type of roof do you have? Pitched or flat?
3. Need: Lower electricity costs, more independence, or both?
4. Timing: When would you ideally want the system installed?
5. Budget: Have you thought about a budget range yet?
6. Decision maker: Is anyone else involved in the decision?
7. Main concern: What is your biggest concern at the moment?

Return strict JSON only."""


def _parse_json_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"parse_error": "Gemini did not return valid JSON.", "raw": text}


async def _generate_json(system_prompt: str, user_payload: dict[str, Any], temperature: float) -> dict[str, Any]:
    if not settings.gemini_api_key:
        return {
            "mock": True,
            "call_ready": True,
            "missing_fields": ["owner_status", "roof_type", "timeline", "budget_range"],
            "opening_line": "Hallo, ich melde mich kurz zu Ihrer Solar-Anfrage und möchte die wichtigsten Eckdaten klären.",
            "prioritized_questions": [
                "Besitzen Sie die Immobilie selbst?",
                "Ist das Dach eher geneigt oder flach?",
                "Wann wäre eine Umsetzung für Sie ideal?",
                "Haben Sie bereits eine Budgetspanne im Kopf?",
            ],
            "risk_notes": ["Gemini API key is not configured; using local development response."],
            "next_action": "schedule_call",
            "qualification_schema": {
                "language": "de",
                "owner_status": None,
                "roof_type": None,
                "need": None,
                "timeline": None,
                "budget_range": None,
                "decision_maker": None,
                "main_concern": None,
                "best_contact_method": None,
                "follow_up_permission": None,
                "confidence_score": 0.0,
            },
        }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
    data = response.json()
    text = "".join(
        part.get("text", "")
        for part in data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )
    return _parse_json_text(text)


async def create_call_plan(lead: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task": "Create the Agent 1 call preparation for this solar lead.",
        "lead": lead,
        "required_output": {
            "call_ready": "boolean",
            "missing_fields": ["string"],
            "opening_line": "string",
            "prioritized_questions": ["string"],
            "risk_notes": ["string"],
            "next_action": "schedule_call | request_info | do_not_contact",
            "qualification_schema": {
                "language": None,
                "owner_status": None,
                "roof_type": None,
                "need": None,
                "timeline": None,
                "budget_range": None,
                "decision_maker": None,
                "main_concern": None,
                "best_contact_method": None,
                "follow_up_permission": None,
                "confidence_score": None,
            },
        },
    }
    return await _generate_json(AGENT_SYSTEM_PROMPT, payload, temperature=0.25)


async def extract_qualification(lead_id: str, transcript: str) -> dict[str, Any]:
    system_prompt = (
        "You are the Qualification Agent for Solar Lead OS. Extract structured "
        "qualification JSON from the transcript. Return strict JSON only."
    )
    payload = {
        "lead_id": lead_id,
        "transcript": transcript,
        "required_fields": [
            "lead_id",
            "language",
            "owner_status",
            "roof_type",
            "need",
            "timeline",
            "budget_range",
            "decision_maker",
            "main_concern",
            "best_contact_method",
            "follow_up_permission",
            "objections",
            "call_outcome",
            "confidence_score",
        ],
    }
    result = await _generate_json(system_prompt, payload, temperature=0.1)
    result.setdefault("lead_id", lead_id)
    result.setdefault("transcript", transcript)
    return result
