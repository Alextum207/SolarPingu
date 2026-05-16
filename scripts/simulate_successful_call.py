from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.models import SolarLeadIntake
from app.services import calendar, email


def _latest_lead_id() -> str:
    leads = db.list_agentic_leads(limit=1)
    if not leads:
        raise SystemExit("No agentic leads found. Submit the intake form first.")
    return str(leads[0]["lead_id"])


def _demo_turns(lead: SolarLeadIntake) -> list[dict[str, str]]:
    first_name = lead.name.strip().split()[0] if lead.name.strip() else "Anna"
    return [
        {
            "role": "agent",
            "speaker": "Agent",
            "text": f"Hallo {first_name}, hier ist Solar Lead OS. Ich habe Ihre Anfrage vor mir und moechte vor allem verstehen, was Ihnen wichtig ist.",
            "lang": "de-DE",
        },
        {
            "role": "customer",
            "speaker": "Kunde",
            "text": "Mir geht es vor allem darum, die Stromkosten zu senken. Ich bin aber unsicher, ob sich ein Speicher wirklich lohnt.",
            "lang": "de-DE",
        },
        {
            "role": "agent",
            "speaker": "Agent",
            "text": "Das ist ein sinnvoller Punkt. Den Speicher sollten wir nicht pauschal verkaufen, sondern anhand Verbrauch und Dach vor Ort sauber pruefen.",
            "lang": "de-DE",
        },
        {
            "role": "customer",
            "speaker": "Kunde",
            "text": "Genau, ich will keine technische Show, sondern wissen, ob das fuer mein Haus wirklich Sinn macht.",
            "lang": "de-DE",
        },
        {
            "role": "agent",
            "speaker": "Agent",
            "text": "Dann ist der beste naechste Schritt ein finales Vor-Ort-Planungsgespraech mit dem Handwerker. Ich wuerde einen der naechsten freien Termine dafuer vormerken.",
            "lang": "de-DE",
        },
    ]


def _qualification() -> dict[str, Any]:
    return {
        "language": "de",
        "owner_status": "owner",
        "roof_type": "pitched_or_to_verify",
        "need": "lower_costs_and_independence",
        "main_concern": "customer wants proof that battery and system design make sense for the specific house",
        "objections": ["too much technical detail", "battery ROI uncertainty", "wants practical in-person validation"],
        "buying_readiness": "high if installer validates roof, consumption and battery sizing in person",
        "desired_outcome": "final in-person planning meeting with installer",
        "call_outcome": "ready_to_book",
        "confidence_score": 0.88,
        "missing_fields": ["exact_roof_area", "annual_consumption", "preferred_installer_slot"],
    }


def _planning_context(stored: dict[str, Any]) -> dict[str, Any]:
    try:
        slots = [
            slot.model_dump(mode="json")
            for slot in calendar.get_available_slots(max_slots=3)
        ]
    except Exception:
        slots = []
    return {
        "lead": stored.get("intake"),
        "solar": stored.get("solar"),
        "profitability": stored.get("profitability"),
        "offer": stored.get("offer"),
        "handoff": stored.get("handoff"),
        "available_slots": slots,
    }


def simulate_successful_call(lead_id: str) -> dict[str, Any]:
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        raise SystemExit(f"Lead not found: {lead_id}")

    lead = SolarLeadIntake.model_validate(stored["intake"])
    turns = _demo_turns(lead)
    transcript = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in turns)
    qualification = _qualification()
    summary = (
        "Der Kunde moechte keine technische Ueberladung, sondern Sicherheit, ob PV "
        "und Speicher fuer das konkrete Haus wirtschaftlich Sinn machen. Die Sorge "
        "Speicher-ROI wurde aufgenommen. Naechster Schritt: finales Vor-Ort-"
        "Planungsgespraech mit Handwerker anhand Agent-2-Plan und freier Termine."
    )
    voice_result = {
        "intent": "ready_to_book",
        "response_text": "Der Kunde ist bereit fuer ein finales Vor-Ort-Planungsgespraech.",
    }
    summary_mail = email.send_conversation_summary(
        lead_id=lead_id,
        lead_name=lead.name,
        lead_email=str(lead.email),
        lead_phone=lead.phone,
        source="Simulierter Twilio-Testcall",
        transcript=transcript,
        conversation_turns=turns,
        qualification=qualification,
        voice_result=voice_result,
        call_summary=summary,
        planning_context=_planning_context(stored),
    )

    voice = stored.get("voice") or {}
    voice["twilio_conversation"] = {
        "call_sid": "SIMULATED_CALL",
        "session_id": "SIMULATED_SESSION",
        "turns": turns,
        "transcript": transcript,
        "qualification": qualification,
        "summary": summary,
        "summary_email": summary_mail,
        "simulated": True,
    }
    voice["customer_call_status"] = "twilio_call_completed"
    voice["provider"] = voice.get("provider") or "twilio"

    db.add_vapi_event(
        lead_id=lead_id,
        call_id="SIMULATED_CALL",
        event_type="twilio_simulated_call_completed",
        payload={"turns": turns, "qualification": qualification, "summary": summary},
    )
    db.update_agentic_artifacts(lead_id, status="twilio_call_completed", voice=voice)
    return {
        "lead_id": lead_id,
        "status": "twilio_call_completed",
        "summary_email_status": summary_mail.get("status"),
        "turn_count": len(turns),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a successful Twilio call for a lead.")
    parser.add_argument("--lead-id", default=None, help="Lead ID to update. Defaults to newest agentic lead.")
    args = parser.parse_args()
    lead_id = args.lead_id or _latest_lead_id()
    result = simulate_successful_call(lead_id)
    print(result)


if __name__ == "__main__":
    main()
