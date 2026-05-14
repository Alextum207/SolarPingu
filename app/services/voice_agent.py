from __future__ import annotations

from app.models import ProfitabilityDecision, SolarLeadIntake, VoiceAgentResult
from app.services import email, gemini


def _fallback_voice(lead: SolarLeadIntake, transcript: str) -> VoiceAgentResult:
    lowered = transcript.lower()
    if any(word in lowered for word in ["abschließen", "passt", "machen wir", "deal"]):
        intent = "closed"
        response = (
            f"Perfekt, {lead.name}. Ich halte fest: Wir gehen mit dem nächsten Schritt weiter. "
            "Das Team bekommt jetzt die Zusammenfassung und meldet sich mit den finalen Details."
        )
        status = "closed"
        notify = True
    elif any(word in lowered for word in ["preis", "teuer", "finanzierung", "kosten"]):
        intent = "objection"
        response = (
            "Guter Punkt. Der Preis hängt vor allem an Anlagengröße, Speicher und Dachdetails. "
            "Der Vorteil ist: Wir prüfen zuerst die Wirtschaftlichkeit, bevor jemand Zeit in einen schlechten Termin steckt."
        )
        status = "objection_handled"
        notify = False
    elif any(word in lowered for word in ["termin", "wann", "buchen"]):
        intent = "ready_to_book"
        response = "Sehr gut. Dann ist der nächste sinnvolle Schritt ein kurzer Termin zur finalen Dach- und Verbrauchsprüfung."
        status = "ready_to_book"
        notify = False
    elif any(word in lowered for word in ["stopp", "kein interesse", "nicht anrufen"]):
        intent = "opt_out"
        response = "Verstanden. Ich notiere, dass kein weiterer Kontakt gewünscht ist."
        status = "opt_out"
        notify = False
    else:
        intent = "question"
        response = (
            "Kurz gesagt: Wir prüfen erst die technische und wirtschaftliche Eignung, "
            "erstellen daraus eine klare Preisrange und schlagen nur dann einen Termin vor, wenn das Projekt profitabel wirkt."
        )
        status = "voice_answered"
        notify = False
    return VoiceAgentResult(
        lead_id=lead.lead_id or "",
        intent=intent,
        response_text=response,
        next_status=status,
        staff_notification_required=notify,
    )


async def answer_from_transcript(
    lead: SolarLeadIntake,
    transcript: str,
    profitability: ProfitabilityDecision | None = None,
) -> VoiceAgentResult:
    fallback = _fallback_voice(lead, transcript).model_dump()
    result = await gemini.generate_structured_json(
        system_prompt=(
            "You are a concise German voice sales agent for Solar Lead OS. "
            "Sound natural like a short NotebookLM-style explanation. Classify intent "
            "as question, objection, ready_to_book, opt_out, or closed. Return strict JSON."
        ),
        payload={
            "lead": lead.model_dump(),
            "profitability": profitability.model_dump() if profitability else None,
            "transcript": transcript,
            "required_shape": fallback,
        },
        temperature=0.25,
        fallback=fallback,
    )
    voice = VoiceAgentResult.model_validate(result)
    if voice.staff_notification_required:
        email.notify_staff(
            lead_id=voice.lead_id,
            message=f"Lead {voice.lead_id} wurde im Voice-Flow als geschlossen erkannt.",
        )
    return voice
