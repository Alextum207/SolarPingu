import base64
import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import BusinessCandidate, ProjectDecision, VisionAnalysis
from app.services.solar import CachedImage


class FeatherlessVisionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def analyze_roof_image(
        self,
        image: CachedImage | None,
        business: BusinessCandidate,
        decision: ProjectDecision,
    ) -> VisionAnalysis:
        if image is None:
            return vision_warning("Roof image unavailable; vision analysis skipped")
        if not self.settings.featherless_api_key:
            return vision_warning("Featherless API key missing; vision analysis skipped")

        encoded_image = base64.b64encode(image.content).decode("ascii")
        data_url = f"data:{image.media_type};base64,{encoded_image}"
        payload = {
            "model": self.settings.featherless_vision_model,
            "temperature": 0.1,
            "max_tokens": 350,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You analyze Google Maps satellite roof images for German "
                        "commercial solar lead triage. Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Estimate visible commercial rooftop solar potential. "
                                "Return keys: visualSolarPotentialScore as 0-1, "
                                "roofType, blockers array, confidence as 0-1. "
                                f"Business: {business.businessName}. "
                                f"Address: {business.address}. "
                                f"Agent 2 estimate: {decision.estimatedKwPeak:.1f} kWp, "
                                f"{decision.panelCount} panels."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
        }

        try:
            timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.settings.featherless_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.featherless_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return parse_featherless_vision_text(str(content))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ):
            return vision_warning("Featherless vision request failed; Agent 2 decision used")


def parse_featherless_vision_text(text: str) -> VisionAnalysis:
    parsed = _parse_json_text(text)
    score = _coerce_score(
        parsed.get("visualSolarPotentialScore", parsed.get("score")),
        field_name="visualSolarPotentialScore",
    )
    confidence = _coerce_score(parsed.get("confidence", 0.65), field_name="confidence")
    roof_type = str(parsed.get("roofType") or parsed.get("roof_type") or "unknown")
    blockers = parsed.get("blockers", parsed.get("obstacles", []))
    return VisionAnalysis(
        visualSolarPotentialScore=score,
        roofType=roof_type[:80] or "unknown",
        blockers=blockers,
        confidence=confidence,
    )


def vision_warning(message: str) -> VisionAnalysis:
    return VisionAnalysis(
        visualSolarPotentialScore=0,
        roofType="unknown",
        blockers=[],
        confidence=0,
        warning=message,
    )


def _parse_json_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start:end])

    if not isinstance(parsed, dict):
        raise ValueError("vision output must be a JSON object")
    return parsed


def _coerce_score(value: Any, field_name: str) -> float:
    if value is None:
        raise ValueError(f"{field_name} is required")
    text = str(value).strip()
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    score = float(text)
    if is_percent or score > 1:
        score = score / 100
    return max(0, min(1, score))
