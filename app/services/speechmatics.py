from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from app.config import settings


SPEECHMATICS_JOBS_URL = "https://asr.api.speechmatics.com/v2/jobs/"

SOLAR_CUSTOM_VOCAB: list[str | dict[str, list[str] | str]] = [
    "SolarPingu",
    "Photovoltaik",
    "PV Anlage",
    {"content": "kWp", "sounds_like": ["Kilowatt Peak", "k W p"]},
    "Kilowatt Peak",
    "Wallbox",
    "Stromspeicher",
    "Batteriespeicher",
    "Eigenverbrauch",
    "Einspeiseverguetung",
    "Satteldach",
    "Flachdach",
    "Dachneigung",
    "Dachausrichtung",
    "Verschattung",
    "Amortisation",
]


def _job_config(lead_id: str, recording_url: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "type": "transcription",
        "transcription_config": {
            "language": "de",
            "operating_point": "enhanced",
            "diarization": "speaker",
            "enable_entities": True,
            "additional_vocab": SOLAR_CUSTOM_VOCAB,
        },
        "notification_config": [
            {
                "url": f"{settings.public_base_url}/webhooks/speechmatics",
                "contents": ["transcript"],
            }
        ],
        "tracking": {"lead_id": lead_id},
    }
    if recording_url:
        config["fetch_data"] = {"url": recording_url}
    return config


async def submit_recording_url(lead_id: str, recording_url: str) -> dict[str, Any]:
    if not settings.speechmatics_api_key:
        return {
            "id": f"mock-{uuid4().hex}",
            "mock": True,
            "lead_id": lead_id,
            "recording_url": recording_url,
        }

    headers = {"Authorization": f"Bearer {settings.speechmatics_api_key}"}
    files = {"config": (None, json.dumps(_job_config(lead_id, recording_url)))}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SPEECHMATICS_JOBS_URL, headers=headers, files=files)
        response.raise_for_status()
    return response.json()


async def submit_audio_file(lead_id: str, filename: str, content: bytes) -> dict[str, Any]:
    if not settings.speechmatics_api_key:
        return {"id": f"mock-{uuid4().hex}", "mock": True, "lead_id": lead_id, "filename": filename}

    headers = {"Authorization": f"Bearer {settings.speechmatics_api_key}"}
    files = {
        "config": (None, json.dumps(_job_config(lead_id, None))),
        "data_file": (filename, content),
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(SPEECHMATICS_JOBS_URL, headers=headers, files=files)
        response.raise_for_status()
    return response.json()


def transcript_from_callback(payload: dict[str, Any]) -> tuple[str, str]:
    artifacts = callback_artifacts(payload)
    return artifacts["lead_id"], artifacts["transcript"]


def callback_artifacts(payload: dict[str, Any]) -> dict[str, Any]:
    lead_id = (
        payload.get("job", {}).get("tracking", {}).get("lead_id")
        or payload.get("tracking", {}).get("lead_id")
        or payload.get("lead_id")
        or ""
    )
    turns = conversation_turns_from_callback(payload)
    if "transcript" in payload and isinstance(payload["transcript"], str):
        transcript = payload["transcript"]
    else:
        transcript = transcript_from_results(payload.get("results"))

    if not transcript:
        transcript = json.dumps(payload, ensure_ascii=False)

    return {
        "lead_id": lead_id,
        "transcript": transcript,
        "conversation_turns": turns,
        "low_confidence_terms": low_confidence_terms(payload.get("results")),
    }


def transcript_from_results(results: Any) -> str:
    if not isinstance(results, list):
        return ""
    text = ""
    for result in results:
        content = _result_content(result)
        if content:
            text = _append_token(text, content)
    return text


def conversation_turns_from_callback(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    confidences: list[float] = []

    def flush() -> None:
        nonlocal current, confidences
        if not current:
            return
        text = str(current.get("text") or "").strip()
        if text:
            current["text"] = text
            if confidences:
                current["confidence"] = round(sum(confidences) / len(confidences), 3)
            turns.append(current)
        current = None
        confidences = []

    for result in results:
        content = _result_content(result)
        if not content:
            continue
        speaker = _result_speaker(result)
        if current is None or current.get("speaker") != speaker:
            flush()
            current = {
                "speaker": speaker,
                "start_time": result.get("start_time"),
                "end_time": result.get("end_time"),
                "text": "",
            }
        current["text"] = _append_token(str(current.get("text") or ""), content)
        current["end_time"] = result.get("end_time", current.get("end_time"))
        confidence = _result_confidence(result)
        if confidence is not None:
            confidences.append(confidence)
    flush()
    return turns


def low_confidence_terms(results: Any, threshold: float = 0.75) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    terms: list[dict[str, Any]] = []
    for result in results:
        confidence = _result_confidence(result)
        content = _result_content(result)
        if confidence is not None and confidence < threshold and content:
            terms.append(
                {
                    "content": content,
                    "confidence": confidence,
                    "speaker": _result_speaker(result),
                    "start_time": result.get("start_time"),
                }
            )
    return terms[:20]


def _result_content(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    alternatives = result.get("alternatives") or []
    if not alternatives:
        return ""
    content = alternatives[0].get("content")
    return str(content) if content else ""


def _result_speaker(result: Any) -> str:
    if not isinstance(result, dict):
        return "UU"
    alternatives = result.get("alternatives") or []
    if alternatives and alternatives[0].get("speaker"):
        return str(alternatives[0]["speaker"])
    return str(result.get("speaker") or "UU")


def _result_confidence(result: Any) -> float | None:
    if not isinstance(result, dict):
        return None
    alternatives = result.get("alternatives") or []
    if not alternatives or alternatives[0].get("confidence") is None:
        return None
    try:
        return float(alternatives[0]["confidence"])
    except (TypeError, ValueError):
        return None


def _append_token(text: str, token: str) -> str:
    token = token.strip()
    if not token:
        return text
    if not text or token in ".,!?;:%)]}":
        return f"{text}{token}"
    if text.endswith(("(", "[", "{")):
        return f"{text}{token}"
    return f"{text} {token}"
