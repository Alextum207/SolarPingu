from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from app.config import settings


SPEECHMATICS_JOBS_URL = "https://asr.api.speechmatics.com/v2/jobs/"


def _job_config(lead_id: str, recording_url: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "type": "transcription",
        "transcription_config": {
            "language": "de",
            "operating_point": "enhanced",
            "diarization": "speaker",
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
    lead_id = (
        payload.get("job", {}).get("tracking", {}).get("lead_id")
        or payload.get("tracking", {}).get("lead_id")
        or payload.get("lead_id")
        or ""
    )
    if "transcript" in payload and isinstance(payload["transcript"], str):
        return lead_id, payload["transcript"]

    results = payload.get("results")
    if isinstance(results, list):
        parts: list[str] = []
        for result in results:
            alternatives = result.get("alternatives") or []
            if alternatives:
                content = alternatives[0].get("content")
                if content:
                    parts.append(content)
        return lead_id, " ".join(parts)

    return lead_id, json.dumps(payload, ensure_ascii=False)
