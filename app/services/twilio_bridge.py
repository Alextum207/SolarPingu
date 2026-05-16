from __future__ import annotations

import html
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlencode
import unicodedata
from typing import Any

import httpx
from fastapi import WebSocket

from app import db
from app.config import settings
from app.models import SolarLeadIntake
from app.services import calendar, email, gemini, installers


def is_configured() -> bool:
    return settings.twilio_configured


def _public_ws_url(path: str) -> str:
    base = settings.public_base_url
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return f"{base.rstrip('/')}{path}"


def _public_http_url(path: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}{path}"


def _local_fallback_response(
    state: dict[str, Any],
    prompt: str,
    lang: str,
    business_case: dict[str, Any] | None = None,
) -> str:
    text = _normalize_for_matching(
        " ".join(
            turn["text"]
            for turn in state.get("turns", [])
            if turn.get("role") == "customer"
        )
    )
    current = _normalize_for_matching(prompt)
    facts = _qualification_flags(text)
    german = lang.startswith("de") or any(
        word in text for word in ["ich", "bin", "eigentumer", "dach", "umsetzung"]
    )
    wants_repeat = any(word in current for word in ["horst", "hear me", "hallo"])
    concern_words = [
        "sorge",
        "angst",
        "unsicher",
        "teuer",
        "lohnt",
        "lohnt sich",
        "rentiert",
        "finanzierung",
        "speicher",
        "e-auto",
        "auto",
        "wallbox",
        "laden",
        "kosten",
    ]
    has_concern = any(word in current for word in concern_words)
    ev_concern = _has_ev_concern(current) or _has_ev_concern(text)
    annual_km = _extract_annual_km(text)
    rough_case = _spoken_business_case(business_case or {})

    if german:
        if wants_repeat and len(state.get("turns", [])) <= 2:
            return "Ja, ich hoere Sie. Ich habe Ihre Anfrage vor mir und gehe gern konkret auf Ihre Solarfrage ein."
        if has_concern and ev_concern and annual_km is None:
            return (
                "Ja, beim E-Auto entscheidet vor allem Ihre Fahrleistung und wann Sie laden. "
                "Mit Solarstrom sparen Sie gegenueber oeffentlichem Laden oft mehrere Euro pro 100 Kilometer. "
                "Wie viele Kilometer fahren Sie grob pro Jahr?"
            )
        if has_concern and ev_concern and annual_km is not None:
            return (
                f"{_spoken_ev_savings(business_case or {}, annual_km, german=True)} "
                "Damit kann sich die Kombination aus PV, Speicher und Autoladen gut lohnen, "
                "wenn ein relevanter Teil des Ladens zuhause passiert."
            )
        if has_concern:
            return (
                f"Ja, genau diese Rentabilitaetsfrage ist wichtig. {rough_case} "
                "Beim E-Auto wird es besonders interessant, wenn Sie viel tagsueber oder mit Speicher laden. "
                "Der Termin ist dann nicht der Start, sondern die Absicherung dieser Rechnung."
            )
        if facts["owner"] and facts["timeline"] and not facts["budget"]:
            return "Das passt grundsaetzlich gut. Was waere fuer Sie die groesste Sorge, bevor Sie einen Vor-Ort-Planungstermin zusagen?"
        if facts["owner"] and facts["timeline"] and facts["budget"] and not facts["roof"]:
            return "Danke, das reicht fuer den naechsten Schritt. Ich wuerde jetzt auf einen Vor-Ort-Termin mit dem Handwerker gehen, damit die Gesamtplanung verbindlich wird. Passt Ihnen einer der naechsten freien Termine?"
        if facts["owner"] and facts["timeline"] and facts["budget"] and facts["roof"]:
            return "Sehr gut, dann wuerde ich das nicht weiter zerreden. Der sinnvolle naechste Schritt ist ein Vor-Ort-Planungsgespraech mit dem Handwerker. Darf ich dafuer einen freien Termin vormerken?"
        if facts["owner"] and not facts["timeline"]:
            return "Danke, die Basis ist klar. Was muesste fuer Sie geklaert sein, damit Sie einem Vor-Ort-Termin zustimmen?"
        if facts["timeline"] and not facts["owner"]:
            return "Den Zeitraum habe ich aus dem Formular. Was ist fuer Sie aktuell die groesste Frage oder Sorge bei Solar?"
        return (
            f"Ich ordne es kurz ein: {rough_case} "
            "Welche Annahme ist fuer Sie am wichtigsten zu klaeren: Speicher, Autoladen oder Gesamtpreis?"
        )

    if facts["owner"] and facts["timeline"] and not facts["budget"]:
        return "That sounds like a good basis. What is your biggest concern before agreeing to an in-person planning appointment?"
    if has_concern and ev_concern and annual_km is None:
        return (
            "For the EV case, annual mileage is the key lever. Solar charging can save several euros per "
            "100 kilometers compared with public charging. Roughly how many kilometers do you drive per year?"
        )
    if has_concern and ev_concern and annual_km is not None:
        return (
            f"{_spoken_ev_savings(business_case or {}, annual_km, german=False)} "
            "So PV plus battery can make sense if a meaningful share of charging happens at home."
        )
    if has_concern:
        return (
            f"That return question is exactly the right one. {rough_case} "
            "For an EV, it becomes strongest when charging overlaps with solar production or a battery. "
            "The installer visit should validate the numbers, not replace the explanation."
        )
    if facts["owner"] and facts["timeline"] and facts["budget"] and not facts["roof"]:
        return "That is enough for the next step. I would suggest an in-person installer planning appointment. Should I reserve one of the next available slots?"
    if facts["owner"] and facts["timeline"] and facts["budget"] and facts["roof"]:
        return "Great, then the best next step is an in-person planning appointment with the installer. May I reserve an available slot for you?"
    return "Thanks. What is your biggest concern before we schedule the in-person installer planning appointment?"


def _normalize_for_matching(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    return normalized.encode("ascii", "ignore").decode("ascii")


def _qualification_flags(text: str) -> dict[str, bool]:
    return {
        "owner": any(word in text for word in ["eigentumer", "eigentuemer", "besitzer", "owner"]),
        "timeline": any(
            word in text
            for word in ["monat", "monaten", "sofort", "jahr", "months", "month", "year"]
        ),
        "budget": any(
            word in text
            for word in ["budget", "euro", "eur", "tausend", "k", "finanzierung"]
        ),
        "roof": any(
            word in text
            for word in ["dach", "satteldach", "flachdach", "geneigt", "flat roof", "roof"]
        ),
    }

async def create_customer_call(
    *,
    lead_id: str,
    customer_number: str,
) -> dict[str, Any]:
    if not settings.twilio_configured:
        return {
            "skipped": True,
            "reason": "Twilio is missing TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or TWILIO_FROM_NUMBER.",
        }

    url = f"{settings.twilio_call_url.rstrip('/')}/Accounts/{settings.twilio_account_sid}/Calls.json"
    data = [
        ("To", customer_number),
        ("From", settings.twilio_from_number or ""),
        ("Url", _public_http_url(f"/webhooks/twilio/voice/{lead_id}")),
        ("StatusCallback", _public_http_url(f"/webhooks/twilio/status/{lead_id}")),
        ("StatusCallbackEvent", "initiated"),
        ("StatusCallbackEvent", "ringing"),
        ("StatusCallbackEvent", "answered"),
        ("StatusCallbackEvent", "completed"),
        ("StatusCallbackMethod", "POST"),
    ]
    if settings.twilio_record_calls:
        data.extend(
            [
                ("Record", "true"),
                ("RecordingChannels", "dual"),
                ("RecordingStatusCallback", _public_http_url(f"/webhooks/twilio/recording/{lead_id}")),
                ("RecordingStatusCallbackMethod", "POST"),
                ("RecordingStatusCallbackEvent", "completed"),
            ]
        )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            content=urlencode(data),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(settings.twilio_account_sid or "", settings.twilio_auth_token or ""),
        )
        if response.status_code >= 400:
            return {
                "failed": True,
                "status_code": response.status_code,
                "error": response.text,
            }
    return response.json()


def build_conversation_relay_twiml(lead_id: str, lead: SolarLeadIntake) -> str:
    first_name = html.escape(lead.name.strip().split()[0] if lead.name.strip() else "there")
    ws_url = html.escape(_public_ws_url(f"/ws/twilio/conversation/{lead_id}"))
    greeting = html.escape(
        f"Hi {first_name}, this is SolarPingu. I will match your language. "
        "I just need to confirm a few details about your solar request."
    )
    language = html.escape(settings.twilio_relay_language)
    tts_provider = html.escape(settings.twilio_relay_tts_provider)
    transcription_provider = html.escape(settings.twilio_relay_transcription_provider)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Connect action="'
        + html.escape(_public_http_url(f"/webhooks/twilio/relay-ended/{lead_id}"))
        + '">'
        f'<ConversationRelay url="{ws_url}" welcomeGreeting="{greeting}" '
        'welcomeGreetingInterruptible="speech" interruptible="speech" '
        'reportInputDuringAgentSpeech="speech" events="speaker-events" '
        f'language="{language}">'
        f'<Language code="{language}" ttsProvider="{tts_provider}" '
        f'transcriptionProvider="{transcription_provider}" />'
        f'<Parameter name="lead_id" value="{html.escape(lead_id)}" />'
        f'<Parameter name="lead_name" value="{html.escape(lead.name)}" />'
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )


async def handle_conversation_ws(websocket: WebSocket, lead_id: str) -> None:
    await websocket.accept()
    stored = db.get_agentic_lead(lead_id)
    if stored is None:
        await websocket.close(code=1008)
        return

    lead = SolarLeadIntake.model_validate(stored["intake"])
    state: dict[str, Any] = {
        "lead_id": lead_id,
        "call_sid": None,
        "session_id": None,
        "turns": [],
        "last_lang": settings.twilio_relay_language if settings.twilio_relay_language != "multi" else None,
    }
    try:
        while True:
            message = await websocket.receive_json()
            message_type = str(message.get("type") or "")
            if message_type == "setup":
                state["call_sid"] = message.get("callSid")
                state["session_id"] = message.get("sessionId")
                _store_twilio_voice_event(lead_id, "conversation_setup", message)
            elif message_type == "prompt" and message.get("last", True):
                await _handle_prompt(websocket, lead, stored, state, message)
            elif message_type == "interrupt":
                _store_twilio_voice_event(lead_id, "conversation_interrupt", message)
            elif message_type == "error":
                _store_twilio_voice_event(lead_id, "conversation_error", message)
    except Exception as exc:
        _store_twilio_voice_event(
            lead_id,
            "conversation_ws_closed",
            {"error": str(exc), "state": state},
        )
        await _persist_summary(lead, state)


async def _handle_prompt(
    websocket: WebSocket,
    lead: SolarLeadIntake,
    stored: dict[str, Any],
    state: dict[str, Any],
    message: dict[str, Any],
) -> None:
    prompt = str(message.get("voicePrompt") or "").strip()
    if not prompt:
        return
    lang = str(message.get("lang") or state.get("last_lang") or "multi")
    state["last_lang"] = lang
    state["turns"].append({"role": "customer", "text": prompt, "lang": lang})

    response = await _gemini_call_response(lead, stored, state, prompt, lang)
    state["turns"].append({"role": "agent", "text": response, "lang": lang})
    _store_twilio_voice_event(
        lead.lead_id or "",
        "conversation_turn",
        {"prompt": prompt, "response": response, "lang": lang},
    )
    reply = {
        "type": "text",
        "token": response,
        "last": True,
        "interruptible": True,
        "preemptible": True,
    }
    await websocket.send_json(reply)


async def _gemini_call_response(
    lead: SolarLeadIntake,
    stored: dict[str, Any],
    state: dict[str, Any],
    prompt: str,
    lang: str,
) -> str:
    business_case = _business_case_context(stored, lead)
    fallback = _local_fallback_response(state, prompt, lang, business_case)
    system_prompt = (
        "You are SolarPingu's multilingual closing advisor. Gemini is the reasoning engine. "
        "Mirror the customer's language exactly; if they speak German, answer in German; "
        "if they speak English, answer in English; if they switch language, follow. "
        "Do not mention PDFs, internal demos, Vapi, Twilio, or technical systems. "
        "The form already collected ownership, roof, need, timeline, budget, decision maker, "
        "and main concern. Do not run a technical checklist and do not re-qualify the lead. "
        "Your first goal is to resolve the customer's concern in plain human language. "
        "When they ask if it is worth it, mention 1-2 rough numbers from business_case "
        "such as system size, yearly kWh, price range, yearly value, or payback. Explain "
        "what that means for their specific worry, for example EV charging, battery value, "
        "financing, or risk. For EV or wallbox concerns: if annual kilometers are missing, "
        "ask how many kilometers they drive per year before concluding. If annual kilometers "
        "are known, estimate EV charging savings using business_case.ev_assumptions and say "
        "the rough saving per 100 km and per year. Do not jump straight to an installer appointment. Only after "
        "the concern is acknowledged and roughly quantified, position the on-site meeting "
        "as validation of the calculation and final planning. Use conversation_so_far as "
        "memory; never ask for the same concern again after the customer has stated it. "
        "Ask one concise question at a time. Keep responses under 65 words."
    )
    customer_text = _normalize_for_matching(
        " ".join(
            turn["text"]
            for turn in state.get("turns", [])
            if turn.get("role") == "customer"
        )
    )
    payload = {
        "lead": lead.model_dump(mode="json"),
        "solar": stored.get("solar"),
        "profitability": stored.get("profitability"),
        "offer": stored.get("offer"),
        "business_case": business_case,
        "agent2_plan": _planning_context(stored),
        "conversation_so_far": state["turns"][-8:],
        "known_qualification": _qualification_flags(customer_text),
        "customer_prompt": prompt,
        "detected_language": lang,
        "task": "Return only the next spoken agent reply.",
    }
    return await gemini.generate_text(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.35,
        fallback=fallback,
    )


def _store_twilio_voice_event(lead_id: str, event_type: str, payload: dict[str, Any]) -> None:
    db.add_vapi_event(
        lead_id=lead_id,
        call_id=str(payload.get("callSid") or payload.get("CallSid") or ""),
        event_type=f"twilio_{event_type}",
        payload=payload,
    )


async def _persist_summary(lead: SolarLeadIntake, state: dict[str, Any]) -> None:
    if not state.get("turns"):
        return
    stored = db.get_agentic_lead(lead.lead_id or "")
    transcript = "\n".join(
        f"{turn['role']}: {turn['text']}"
        for turn in state["turns"]
    )
    summary = await gemini.generate_text(
        system_prompt=(
            "Summarize this solar sales phone call in the same language used most by "
            "the customer. Focus on needs, worries, objections handled, buying readiness, "
            "and the next in-person installer planning appointment."
        ),
        payload={"transcript": transcript, "lead": lead.model_dump(mode="json")},
        temperature=0.2,
        fallback=transcript[:700],
    )
    try:
        qualification = await gemini.extract_qualification(lead.lead_id or "", transcript)
    except Exception:
        qualification = {}
    summary_mail = email.send_conversation_summary(
        lead_id=lead.lead_id or "",
        lead_name=lead.name,
        lead_email=str(lead.email),
        lead_phone=lead.phone,
        source="Twilio ConversationRelay",
        transcript=transcript,
        conversation_turns=state["turns"],
        qualification=qualification,
        call_summary=summary,
        planning_context=_planning_context(stored or {}),
    )
    customer_followup = email.send_customer_booking_followup(
        lead,
        summary=summary,
    )
    if stored is not None:
        voice = stored.get("voice") or {}
        voice["twilio_conversation"] = {
            "call_sid": state.get("call_sid"),
            "session_id": state.get("session_id"),
            "turns": state["turns"],
            "summary": summary,
            "qualification": qualification,
            "summary_email": summary_mail,
            "customer_booking_followup": customer_followup,
        }
        db.update_agentic_artifacts(
            lead.lead_id or "",
            status="twilio_call_completed",
            voice=voice,
        )


def status_payload(form: dict[str, Any]) -> dict[str, Any]:
    return {key: str(value) for key, value in form.items()}


def recording_media_url(recording_url: str, extension: str = "mp3") -> str:
    if not recording_url:
        return recording_url
    suffix = PurePosixPath(recording_url.split("?", 1)[0]).suffix
    if suffix:
        return recording_url
    return f"{recording_url}.{extension.lstrip('.')}"


async def download_recording_audio(
    recording_url: str,
    *,
    lead_id: str,
) -> dict[str, Any]:
    media_url = recording_media_url(recording_url, "mp3")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            media_url,
            auth=(settings.twilio_account_sid or "", settings.twilio_auth_token or ""),
        )
        response.raise_for_status()
    content_type = response.headers.get("content-type") or "audio/mpeg"
    extension = "mp3" if "mpeg" in content_type or media_url.endswith(".mp3") else "wav"
    return {
        "filename": f"solar-call-{lead_id}.{extension}",
        "content": response.content,
        "content_type": content_type,
        "media_url": media_url,
    }


def _has_ev_concern(text: str) -> bool:
    return any(
        word in text
        for word in [
            "e-auto",
            "e auto",
            "elektroauto",
            "elektro",
            "auto",
            "wallbox",
            "laden",
            "ladestation",
            "ladesaule",
            "charging",
            "ev",
        ]
    )


def _extract_annual_km(text: str) -> int | None:
    patterns = [
        r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,6})\s*(?:km|kilometer)",
        r"(?:km|kilometer)\s*(\d{1,3}(?:[.\s]\d{3})+|\d{4,6})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = re.sub(r"\D", "", match.group(1))
            if value:
                km = int(value)
                if 1000 <= km <= 100000:
                    return km
    return None


def _business_case_context(stored: dict[str, Any], lead: SolarLeadIntake) -> dict[str, Any]:
    solar = stored.get("solar") or {}
    potential = solar.get("solar_potential") or {}
    profitability = stored.get("profitability") or {}
    offer = stored.get("offer") or {}
    price_range = offer.get("price_range") or {}
    kwp = (
        offer.get("system_size_kwp")
        or profitability.get("estimated_kwp")
        or potential.get("estimated_kwp")
    )
    yearly_kwh = potential.get("yearly_energy_kwh")
    price_min = price_range.get("min") or profitability.get("estimated_price_min")
    price_max = price_range.get("max") or profitability.get("estimated_price_max")
    payback = profitability.get("payback_years")
    estimated_yearly_value = None
    if yearly_kwh:
        self_consumption_rate = 0.65 if lead.battery_interest else 0.45
        avoided_grid_price = 0.32
        export_price = 0.08
        estimated_yearly_value = int(
            yearly_kwh * self_consumption_rate * avoided_grid_price
            + yearly_kwh * (1 - self_consumption_rate) * export_price
        )
    return {
        "system_size_kwp": kwp,
        "yearly_energy_kwh": yearly_kwh,
        "price_min_eur": price_min,
        "price_max_eur": price_max,
        "payback_years": payback,
        "estimated_yearly_value_eur": estimated_yearly_value,
        "includes_battery": offer.get("includes_battery") or lead.battery_interest,
        "ev_or_wallbox_interest": lead.wallbox_interest,
        "ev_assumptions": {
            "ev_kwh_per_100km": 18,
            "public_charging_eur_per_kwh": 0.55,
            "solar_charging_value_eur_per_kwh": 0.15,
            "saving_vs_public_charging_eur_per_100km": 7.2,
        },
        "main_concern": lead.main_concern,
        "assumptions": [
            "Rough phone estimate only; roof, shade, meter cabinet and load profile need installer validation.",
            "EV value depends on charging times, wallbox behavior and battery sizing.",
        ],
    }


def _spoken_business_case(business_case: dict[str, Any]) -> str:
    kwp = business_case.get("system_size_kwp")
    yearly_kwh = business_case.get("yearly_energy_kwh")
    price_min = business_case.get("price_min_eur")
    price_max = business_case.get("price_max_eur")
    payback = business_case.get("payback_years")
    yearly_value = business_case.get("estimated_yearly_value_eur")
    parts = []
    if kwp and yearly_kwh:
        parts.append(f"grob sehen wir etwa {kwp:g} kWp und rund {_format_de_int(yearly_kwh)} kWh pro Jahr")
    if price_min and price_max:
        parts.append(f"eine Investition um {_format_de_int(price_min)} bis {_format_de_int(price_max)} Euro")
    if yearly_value:
        parts.append(f"grob {_format_de_int(yearly_value)} Euro Jahreswert bei gutem Eigenverbrauch")
    if payback:
        parts.append(f"Amortisation grob um {float(payback):g} Jahre")
    if not parts:
        return "Die Wirtschaftlichkeit haengt vor allem an Eigenverbrauch, Dachflaeche, Speichergroesse und Strompreis."
    return "; ".join(parts) + "."


def _spoken_ev_savings(
    business_case: dict[str, Any],
    annual_km: int,
    *,
    german: bool,
) -> str:
    assumptions = business_case.get("ev_assumptions") or {}
    ev_kwh_per_100km = float(assumptions.get("ev_kwh_per_100km") or 18)
    public_price = float(assumptions.get("public_charging_eur_per_kwh") or 0.55)
    solar_value = float(assumptions.get("solar_charging_value_eur_per_kwh") or 0.15)
    saving_per_100km = ev_kwh_per_100km * max(public_price - solar_value, 0)
    yearly_saving = int(round((annual_km / 100) * saving_per_100km / 10) * 10)
    if german:
        return (
            f"Bei grob {_format_de_int(annual_km)} Kilometern pro Jahr braucht das E-Auto etwa "
            f"{_format_de_int((annual_km / 100) * ev_kwh_per_100km)} kWh. "
            f"Gegenueber oeffentlichem Laden sparen Sie mit Solarstrom grob "
            f"{saving_per_100km:.0f} Euro pro 100 Kilometer, also etwa "
            f"{_format_de_int(yearly_saving)} Euro pro Jahr."
        )
    return (
        f"At roughly {annual_km:,} kilometers per year, the EV needs about "
        f"{int((annual_km / 100) * ev_kwh_per_100km):,} kWh. "
        f"Compared with public charging, solar charging can save roughly "
        f"{saving_per_100km:.0f} euros per 100 kilometers, or about "
        f"{yearly_saving:,} euros per year."
    )


def _format_de_int(value: Any) -> str:
    return f"{int(value):,}".replace(",", ".")


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
        "installers": installers.installer_slot_options(max_slots_per_installer=3),
        "call_recording": (stored.get("voice") or {}).get("twilio_recording"),
    }
