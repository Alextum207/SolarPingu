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
        "sonne",
        "wetter",
        "winter",
        "frankfurt",
    ]
    has_concern = any(word in current for word in concern_words)
    current_concerns = _detect_objection_keys(current)
    current_ev_concern = "ev" in current_concerns
    annual_km_current = _extract_annual_km(current)
    mileage_followup = _last_agent_asked_mileage(state) and annual_km_current is not None
    rough_case = _spoken_business_case(business_case or {})
    opening_prompt = _is_opening_prompt(current)

    if german:
        if wants_repeat and len(state.get("turns", [])) <= 2:
            return "Ja, ich hoere Sie. Ich habe Ihre Anfrage vor mir und gehe gern konkret auf Ihre Solarfrage ein."
        if opening_prompt:
            return (
                "Ja, sehr gern. Bevor ich mit Zahlen anfange: Was ist bei Ihnen gerade "
                "die groesste Frage oder Sorge zu Solar?"
            )
        if (current_ev_concern or mileage_followup) and annual_km_current is not None:
            return (
                f"{_spoken_ev_savings(business_case or {}, annual_km_current, german=True)} "
                "Damit kann sich die Kombination aus PV, Speicher und Autoladen gut lohnen, "
                "wenn ein relevanter Teil des Ladens zuhause passiert."
            )
        if len(current_concerns) > 1 and not mileage_followup:
            return _multi_concern_response(current_concerns, business_case or {})
        playbook_response = _objection_playbook_response(current, business_case or {})
        if playbook_response:
            return playbook_response
        if has_concern and current_ev_concern and annual_km_current is None:
            return (
                "Ja, beim E-Auto entscheidet vor allem Ihre Fahrleistung und wann Sie laden. "
                "Mit Solarstrom sparen Sie gegenueber oeffentlichem Laden oft mehrere Euro pro 100 Kilometer. "
                "Wie viele Kilometer fahren Sie grob pro Jahr?"
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
        return "Was ist bei Ihnen gerade die groesste Frage oder Sorge: Gesamtpreis, Speicher, Autoladen oder ob das Dach genug bringt?"

    if opening_prompt:
        return "Absolutely. Before I start with numbers: what is your biggest question or concern about solar right now?"
    if facts["owner"] and facts["timeline"] and not facts["budget"]:
        return "That sounds like a good basis. What is your biggest concern before agreeing to an in-person planning appointment?"
    if (current_ev_concern or mileage_followup) and annual_km_current is not None:
        return (
            f"{_spoken_ev_savings(business_case or {}, annual_km_current, german=False)} "
            "So PV plus battery can make sense if a meaningful share of charging happens at home."
        )
    if has_concern and current_ev_concern and annual_km_current is None:
        return (
            "For the EV case, annual mileage is the key lever. Solar charging can save several euros per "
            "100 kilometers compared with public charging. Roughly how many kilometers do you drive per year?"
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


def _should_ignore_relay_prompt(prompt: str, state: dict[str, Any]) -> bool:
    current = _normalize_for_matching(prompt).strip(" .,!?:;-")
    if not current:
        return True
    backchannels = {
        "thanks",
        "thank you",
        "ok",
        "okay",
        "yes",
        "yeah",
        "mhm",
        "uh huh",
        "danke",
        "dankeschon",
        "dankeschoen",
        "ja",
        "genau",
    }
    if current in backchannels:
        return True
    agent_echo_fragments = [
        "i will match your language",
        "just need to confirm a few",
        "what is your biggest concern",
        "before we schedule",
        "thanks what is your biggest concern",
        "was ist bei ihnen gerade die groesste frage",
        "groesste frage oder sorge",
        "hier ist solarpingu wegen ihrer solaranfrage",
    ]
    if any(fragment in current for fragment in agent_echo_fragments):
        return True
    agent_turns = [
        _normalize_for_matching(turn.get("text", ""))
        for turn in state.get("turns", [])
        if turn.get("role") == "agent"
    ]
    if agent_turns:
        last_agent = agent_turns[-1]
        if len(current) >= 10 and (current in last_agent or last_agent in current):
            return True
        current_words = set(current.split())
        agent_words = set(last_agent.split())
        if len(current_words) >= 4:
            overlap = len(current_words & agent_words) / max(len(current_words), 1)
            if overlap >= 0.75:
                return True
    if not any(turn.get("role") == "customer" for turn in state.get("turns", [])):
        startup_noise = ["time", "ah", "um", "hm", "hmm"]
        if current in startup_noise or len(current.split()) <= 2 and current in {"what is", "thanks"}:
            return True
    return False


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
    first_name = html.escape(lead.name.strip().split()[0] if lead.name.strip() else "danke")
    ws_url = html.escape(_public_ws_url(f"/ws/twilio/conversation/{lead_id}"))
    greeting = html.escape(
        f"Hallo {first_name}, hier ist SolarPingu wegen Ihrer Solaranfrage. "
        "Was ist bei Ihnen gerade die groesste Frage oder Sorge zu Solar?"
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
        'welcomeGreetingInterruptible="none" interruptible="speech" '
        'interruptSensitivity="low" reportInputDuringAgentSpeech="none" '
        'ignoreBackchannel="true" speechTimeout="1200" events="speaker-events" '
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
    if _should_ignore_relay_prompt(prompt, state):
        _store_twilio_voice_event(
            lead.lead_id or "",
            "conversation_ignored_prompt",
            {"prompt": prompt, "lang": lang, "reason": "backchannel_or_agent_echo"},
        )
        return
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
        "interruptible": False,
        "preemptible": False,
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
        "At the very beginning, when the customer only says they are ready or you can start, "
        "do not recite system size, yearly kWh, savings, payback, prices, or any other numbers. "
        "First ask what their biggest concern or question is. "
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
        "For concerns about Frankfurt, weather, winter, rain, clouds, or whether the sun shines "
        "enough, explain that PV does not need constant direct sun and also works with diffuse "
        "daylight. Use the annual kWh estimate as the anchor, then ask if they worry more about "
        "winter days or the full-year yield. "
        "If the customer mentions several concerns in one answer, name the concerns briefly, "
        "handle one of them, and ask which one to unpack next. If they bring up a new second "
        "concern later, answer the new concern instead of returning to the first one. "
        "Use objection_playbook when it matches. Insert the actual numbers and end with a "
        "small check question like 'Ist genau das Ihre Hauptsorge?' or 'Soll ich die Annahme "
        "kurz genauer aufdroeseln?' It is better to ask once more than to close too early. "
        "For phone audio, avoid dense written numbers like 10.672 kWh or 23.320 Euro. Prefer "
        "spoken approximate wording from business_case.spoken, for example 'rund 8 tausend "
        "500 Kilowattstunden' or '23 tausend bis 27 tausend Euro'. "
        "Ask one concise question at a time. Keep responses under 80 words."
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
        "objection_playbook": _objection_playbook(business_case),
        "agent2_plan": _planning_context(stored),
        "conversation_so_far": state["turns"][-8:],
        "known_qualification": _qualification_flags(customer_text),
        "current_concerns": _detect_objection_keys(_normalize_for_matching(prompt)),
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


def _last_agent_asked_mileage(state: dict[str, Any]) -> bool:
    for turn in reversed(state.get("turns", [])):
        if turn.get("role") != "agent":
            continue
        text = _normalize_for_matching(str(turn.get("text") or ""))
        return "wie viele kilometer" in text or "how many kilometers" in text
    return False


def _is_opening_prompt(text: str) -> bool:
    return any(
        phrase in text
        for phrase in [
            "wir konnen anfangen",
            "wir koennen anfangen",
            "ich bin bereit",
            "du bereit bist",
            "kann losgehen",
            "leg los",
            "fangen wir an",
            "starten",
        ]
    )


def _extract_annual_km(text: str) -> int | None:
    patterns = [
        r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,6})\s*(?:km|kilometer)",
        r"(?:km|kilometer)\s*(\d{1,3}(?:[.\s]\d{3})+|\d{4,6})",
        r"(?:circa|ca|ungefahr|ungefaehr|rund|etwa|grob)?\s*(\d{4,6})(?:\s*(?:im|pro)\s*jahr)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = re.sub(r"\D", "", match.group(1))
            if value:
                km = int(value)
                if 1000 <= km <= 100000:
                    return km
    word_km = _extract_annual_km_word(text)
    if word_km is not None:
        return word_km
    return None


def _extract_annual_km_word(text: str) -> int | None:
    compact = re.sub(r"[\s-]+", "", text)
    number_words = {
        "eintausend": 1000,
        "zweitausend": 2000,
        "dreitausend": 3000,
        "viertausend": 4000,
        "funftausend": 5000,
        "fuenftausend": 5000,
        "sechstausend": 6000,
        "siebentausend": 7000,
        "achttausend": 8000,
        "neuntausend": 9000,
        "zehntausend": 10000,
        "elftausend": 11000,
        "zwolftausend": 12000,
        "zwoelftausend": 12000,
        "dreizehntausend": 13000,
        "vierzehntausend": 14000,
        "funfzehntausend": 15000,
        "fuenfzehntausend": 15000,
        "sechzehntausend": 16000,
        "siebzehntausend": 17000,
        "achtzehntausend": 18000,
        "neunzehntausend": 19000,
        "zwanzigtausend": 20000,
    }
    for word, km in number_words.items():
        if word in compact:
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
    lead_score = profitability.get("score")
    modules = offer.get("modules") or offer.get("module_count")
    if modules is None and kwp:
        modules = max(1, round(float(kwp) / 0.4))
    ghosting_risk = stored.get("ghosting_risk")
    if ghosting_risk is None:
        ghosting_risk = (stored.get("handoff") or {}).get("ghosting_risk")
    if ghosting_risk is None and lead_score is not None:
        ghosting_risk = max(0, min(100, 100 - int(lead_score)))
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
        "lead_score": lead_score,
        "module_count": modules,
        "ghosting_risk": ghosting_risk,
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
        "spoken": _spoken_value_context(
            kwp=kwp,
            yearly_kwh=yearly_kwh,
            price_min=price_min,
            price_max=price_max,
            payback=payback,
            yearly_value=estimated_yearly_value,
            modules=modules,
            lead_score=lead_score,
            ghosting_risk=ghosting_risk,
        ),
    }


def _spoken_business_case(business_case: dict[str, Any]) -> str:
    spoken = business_case.get("spoken") or _spoken_value_context(
        kwp=business_case.get("system_size_kwp"),
        yearly_kwh=business_case.get("yearly_energy_kwh"),
        price_min=business_case.get("price_min_eur"),
        price_max=business_case.get("price_max_eur"),
        payback=business_case.get("payback_years"),
        yearly_value=business_case.get("estimated_yearly_value_eur"),
        modules=business_case.get("module_count"),
        lead_score=business_case.get("lead_score"),
        ghosting_risk=business_case.get("ghosting_risk"),
    )
    parts = []
    if spoken.get("kwp") and spoken.get("yearly_kwh"):
        parts.append(
            f"grob sehen wir etwa {spoken['kwp']} Kilowatt Peak und rund "
            f"{spoken['yearly_kwh']} Kilowattstunden pro Jahr"
        )
    if spoken.get("price_min") and spoken.get("price_max"):
        parts.append(f"eine Investition um {spoken['price_min']} bis {spoken['price_max']} Euro")
    if spoken.get("yearly_saving"):
        parts.append(f"grob {spoken['yearly_saving']} Euro Jahreswert bei gutem Eigenverbrauch")
    if spoken.get("payback"):
        parts.append(f"Amortisation grob um {spoken['payback']} Jahre")
    if not parts:
        return "Die Wirtschaftlichkeit haengt vor allem an Eigenverbrauch, Dachflaeche, Speichergroesse und Strompreis."
    return "; ".join(parts) + "."


def _objection_playbook(business_case: dict[str, Any]) -> dict[str, str]:
    values = _playbook_values(business_case)
    return {
        "too_expensive": (
            f"Ja, die Investition liegt voraussichtlich zwischen {values['price_min']} Euro "
            f"und {values['price_max']} Euro. Aber das ist kein verlorenes Geld, sondern "
            f"eine Investition, die sich durch die jaehrliche Ersparnis von {values['yearly_saving']} "
            "Euro Schritt fuer Schritt selbst abbezahlt."
        ),
        "payback": (
            f"Ihre Anlage hat eine Amortisationszeit von grob {values['payback']} Jahren. "
            "Moderne Module halten typischerweise 25 bis 30 Jahre; danach produziert die Anlage "
            "noch viele Jahre sehr guenstigen Strom."
        ),
        "roof_quality": (
            f"Unser System bewertet Ihr Projekt mit einem Lead-Score von {values['lead_score']} Prozent. "
            "Das spricht dafuer, dass Dach und Gegebenheiten grundsaetzlich sehr gut zu Photovoltaik passen."
        ),
        "production": (
            f"Mit einer Anlagengroesse von {values['kwp']} Kilowatt Peak erzeugen Sie voraussichtlich "
            f"{values['yearly_kwh']} Kilowattstunden im Jahr. Ein typisches Einfamilienhaus liegt grob bei "
            "4 tausend Kilowattstunden, also haben Sie Puffer fuer Zukunftsthemen wie Waermepumpe oder E-Auto."
        ),
        "roof_space": (
            f"Fuer diese Leistung rechnen wir grob mit {values['modules']} Modulen. "
            "Basierend auf den Dachdaten nutzt das die verfuegbare Flaeche sinnvoll aus."
        ),
        "energy_prices": (
            f"Die errechnete Ersparnis von {values['yearly_saving']} Euro pro Jahr basiert auf "
            "konservativen Annahmen. Selbst wenn Strompreise schwanken, schuetzt eigener Solarstrom "
            "langfristig vor steigenden Energiekosten."
        ),
        "hesitation": (
            f"Die Zahlen sprechen wirtschaftlich fuer das Projekt; das Ghosting-Risiko liegt intern "
            f"bei etwa {values['ghosting_risk']} Prozent. Ich wuerde Ihnen die wichtigsten Punkte "
            "gern nochmal knapp zusammenfassen, bevor wir ueber den naechsten Schritt sprechen."
        ),
        "hidden_costs": (
            f"Der geschaetzte Preis liegt zwischen {values['price_min']} Euro und {values['price_max']} Euro. "
            "Diese Spanne ist bewusst als Korridor gedacht und puffert typische Punkte wie Geruest, "
            "Zaehlerschrank oder Montage-Details bereits eher mit ab."
        ),
        "resale": (
            f"Eine Anlage, die jaehrlich grob {values['yearly_saving']} Euro Energiekosten spart, "
            "kann den Wert der Immobilie staerken. Fuer Kaeufer ist ein Haus mit niedrigeren "
            "Nebenkosten ein sehr konkretes Argument."
        ),
        "sunlight_region": (
            "In Frankfurt muss nicht immer die Sonne scheinen, damit Photovoltaik funktioniert. "
            "Die Anlage arbeitet auch mit diffusem Tageslicht; entscheidend ist der Ertrag ueber "
            f"das ganze Jahr. Bei Ihren Daten rechnen wir grob mit {values['yearly_kwh']} "
            "Kilowattstunden pro Jahr."
        ),
    }


def _objection_playbook_response(current: str, business_case: dict[str, Any]) -> str | None:
    playbook = _objection_playbook(business_case)
    response: str | None = None
    if any(word in current for word in ["zu teuer", "leisten", "anschaffung", "kosten zu hoch"]):
        response = playbook["too_expensive"]
    elif any(word in current for word in ["amortisiert", "amortisation", "20 jahr", "geld wieder"]):
        response = playbook["payback"]
    elif any(word in current for word in ["passt", "zu klein", "module", "platz", "riesig", "sperrig"]):
        response = playbook["roof_space"]
    elif any(word in current for word in ["mein dach", "dach uberhaupt", "geeignet", "dimensionierung"]):
        response = playbook["roof_quality"]
    elif any(word in current for word in ["genug strom", "haushalt", "netzstrom", "erzeugt"]):
        response = playbook["production"]
    elif any(word in current for word in ["strompreise sinken", "strompreis sinkt", "schongerechnet", "schoengerechnet"]):
        response = playbook["energy_prices"]
    elif any(word in current for word in ["bedenkzeit", "unsicher", "uberlegen", "ueberlegen", "weiss nicht", "weiß nicht"]):
        response = playbook["hesitation"]
    elif any(word in current for word in ["alles drin", "versteckte kosten", "wechselrichter", "montage", "gerust", "geruest"]):
        response = playbook["hidden_costs"]
    elif any(word in current for word in ["haus verkaufe", "verkaufen", "umziehe", "umziehen"]):
        response = playbook["resale"]
    elif any(word in current for word in ["sonne", "sonnig", "scheint", "frankfurt", "wetter", "bewolkt", "regen", "winter"]):
        response = playbook["sunlight_region"]
    if response is None:
        return None
    return f"{response} Ist genau das gerade Ihre Hauptsorge, oder soll ich eine Annahme genauer aufdroeseln?"


def _detect_objection_keys(current: str) -> list[str]:
    checks = [
        ("too_expensive", ["zu teuer", "leisten", "anschaffung", "kosten zu hoch", "gesamtpreis"]),
        ("payback", ["amortisiert", "amortisation", "20 jahr", "geld wieder", "lohnt", "rentiert"]),
        ("roof_space", ["passt", "zu klein", "module", "platz", "riesig", "sperrig"]),
        ("roof_quality", ["mein dach", "dach uberhaupt", "geeignet", "dimensionierung"]),
        ("production", ["genug strom", "haushalt", "netzstrom", "erzeugt"]),
        ("energy_prices", ["strompreise sinken", "strompreis sinkt", "schongerechnet", "schoengerechnet"]),
        ("hesitation", ["bedenkzeit", "unsicher", "uberlegen", "ueberlegen", "weiss nicht", "weiß nicht"]),
        ("hidden_costs", ["alles drin", "versteckte kosten", "wechselrichter", "montage", "gerust", "geruest"]),
        ("resale", ["haus verkaufe", "verkaufen", "umziehe", "umziehen"]),
        ("sunlight_region", ["sonne", "sonnig", "scheint", "frankfurt", "wetter", "bewolkt", "regen", "winter"]),
        ("ev", ["e-auto", "e auto", "elektroauto", "elektro", "auto", "wallbox", "laden", "ladestation", "ladesaule"]),
    ]
    detected = []
    for key, words in checks:
        if any(word in current for word in words):
            detected.append(key)
    return detected


def _multi_concern_response(concerns: list[str], business_case: dict[str, Any]) -> str:
    playbook = _objection_playbook(business_case)
    labels = {
        "too_expensive": "Gesamtpreis",
        "payback": "ob es sich lohnt",
        "roof_space": "Dachflaeche",
        "roof_quality": "Dach-Eignung",
        "production": "Strommenge",
        "energy_prices": "Strompreis-Risiko",
        "hesitation": "Unsicherheit",
        "hidden_costs": "versteckte Kosten",
        "resale": "Hausverkauf",
        "sunlight_region": "Sonne in Frankfurt",
        "ev": "E-Auto-Laden",
    }
    named = [labels.get(concern, concern) for concern in concerns[:3]]
    intro = "Ich hoere da mehrere Punkte: " + ", ".join(named) + ". "
    if "too_expensive" in concerns:
        return intro + playbook["too_expensive"] + " Danach wuerde ich direkt den naechsten Punkt nehmen. Welcher ist Ihnen gerade wichtiger?"
    if "hidden_costs" in concerns:
        return intro + playbook["hidden_costs"] + " Danach koennen wir den zweiten Punkt sauber klaeren. Passt das?"
    if "ev" in concerns:
        return intro + (
            "Beim E-Auto brauche ich eine Zusatzannahme, sonst rechne ich ins Blaue: "
            "Wie viele Kilometer fahren Sie grob pro Jahr?"
        )
    if "sunlight_region" in concerns:
        return intro + playbook["sunlight_region"] + " Ist Ihre Sorge eher der Winter oder ob der Jahresertrag insgesamt reicht?"
    first = concerns[0]
    if first in playbook:
        return intro + playbook[first] + " Soll ich danach den zweiten Punkt genauer aufdroeseln?"
    return intro + "Lassen Sie uns das der Reihe nach machen. Welcher Punkt ist fuer Sie gerade der wichtigste?"


def _playbook_values(business_case: dict[str, Any]) -> dict[str, str]:
    spoken = business_case.get("spoken") or _spoken_value_context(
        kwp=business_case.get("system_size_kwp"),
        yearly_kwh=business_case.get("yearly_energy_kwh"),
        price_min=business_case.get("price_min_eur"),
        price_max=business_case.get("price_max_eur"),
        payback=business_case.get("payback_years"),
        yearly_value=business_case.get("estimated_yearly_value_eur"),
        modules=business_case.get("module_count"),
        lead_score=business_case.get("lead_score"),
        ghosting_risk=business_case.get("ghosting_risk"),
    )
    return {
        "price_min": spoken.get("price_min") or "noch nicht final",
        "price_max": spoken.get("price_max") or "noch nicht final",
        "yearly_saving": spoken.get("yearly_saving") or "noch nicht final",
        "payback": spoken.get("payback") or "noch nicht final",
        "lead_score": spoken.get("lead_score") or "noch nicht final",
        "kwp": spoken.get("kwp") or "noch nicht final",
        "yearly_kwh": spoken.get("yearly_kwh") or "noch nicht final",
        "modules": spoken.get("modules") or "noch nicht final",
        "ghosting_risk": spoken.get("ghosting_risk") or "noch nicht final",
    }


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
        km_spoken = _format_phone_int(annual_km)
        ev_kwh_spoken = _format_phone_int((annual_km / 100) * ev_kwh_per_100km)
        yearly_saving_spoken = _format_phone_int(yearly_saving)
        return (
            f"Bei grob {km_spoken} Kilometern pro Jahr braucht das E-Auto etwa "
            f"{ev_kwh_spoken} Kilowattstunden. "
            f"Gegenueber oeffentlichem Laden sparen Sie mit Solarstrom grob "
            f"{saving_per_100km:.0f} Euro pro 100 Kilometer, also etwa "
            f"{yearly_saving_spoken} Euro pro Jahr."
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


def _format_de_float(value: Any) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".replace(".", ",")


def _spoken_value_context(
    *,
    kwp: Any,
    yearly_kwh: Any,
    price_min: Any,
    price_max: Any,
    payback: Any,
    yearly_value: Any,
    modules: Any,
    lead_score: Any,
    ghosting_risk: Any,
) -> dict[str, str | None]:
    return {
        "kwp": _format_phone_decimal(kwp),
        "yearly_kwh": _format_phone_int(yearly_kwh),
        "price_min": _format_phone_int(price_min),
        "price_max": _format_phone_int(price_max),
        "payback": _format_phone_payback(payback),
        "yearly_saving": _format_phone_int(yearly_value),
        "modules": _format_phone_int(modules, round_large=False),
        "lead_score": _format_phone_int(lead_score, round_large=False),
        "ghosting_risk": _format_phone_int(ghosting_risk, round_large=False),
    }


def _format_phone_int(value: Any, *, round_large: bool = True) -> str | None:
    if value is None:
        return None
    number = int(round(float(value)))
    if round_large and number >= 1000:
        number = int(round(number / 100) * 100)
    if number >= 1000:
        thousands = number // 1000
        rest = number % 1000
        if rest == 0:
            return f"{thousands} tausend"
        return f"{thousands} tausend {rest}"
    return str(number)


def _format_phone_decimal(value: Any) -> str | None:
    if value is None:
        return None
    text = _format_de_float(value)
    return text.replace(",", " Komma ")


def _format_phone_payback(value: Any) -> str | None:
    if value is None:
        return None
    number = float(value)
    if number.is_integer():
        return str(int(number))
    lower = int(number)
    upper = lower + 1
    return f"{lower} bis {upper}"


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
