import hashlib
import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import (
    AddressEvaluationRequest,
    Decision,
    DecisionLayerResult,
    FinancialEstimate,
    GeocodeResult,
    InstallationTimeline,
    LeadScores,
    NextAction,
    OwnerStatus,
    ResourceLevel,
    RoofSuitability,
    SolarPotential,
    model_to_jsonable,
)


class DecisionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def decide(
        self,
        request: AddressEvaluationRequest,
        geocode: GeocodeResult,
        solar: SolarPotential,
        financials: FinancialEstimate,
        scores: LeadScores,
        lead_id: str,
    ) -> DecisionLayerResult:
        assigned_rep = assign_sales_rep(lead_id)

        if self.settings.gemini_api_key:
            gemini_result = await self._try_gemini_decision(
                request=request,
                geocode=geocode,
                solar=solar,
                financials=financials,
                scores=scores,
                assigned_rep=assigned_rep,
            )
            if gemini_result is not None:
                return gemini_result

        return fallback_decision(
            request=request,
            solar=solar,
            financials=financials,
            scores=scores,
            assigned_rep=assigned_rep,
        )

    async def _try_gemini_decision(
        self,
        request: AddressEvaluationRequest,
        geocode: GeocodeResult,
        solar: SolarPotential,
        financials: FinancialEstimate,
        scores: LeadScores,
        assigned_rep: str,
    ) -> DecisionLayerResult | None:
        prompt = _build_gemini_prompt(
            request=request,
            geocode=geocode,
            solar=solar,
            financials=financials,
            scores=scores,
            assigned_rep=assigned_rep,
        )
        endpoint = (
            f"{self.settings.gemini_api_base_url}/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseJsonSchema": _decision_json_schema(),
            },
        }

        try:
            timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    endpoint,
                    headers={
                        "x-goog-api-key": self.settings.gemini_api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                text = _extract_gemini_text(response.json())
                parsed = _parse_json_text(text)
                result = DecisionLayerResult.model_validate(parsed)
                return result.model_copy(update={"assignedRep": assigned_rep})
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ):
            return None


def assign_sales_rep(lead_id: str) -> str:
    digits = "".join(char for char in lead_id if char.isdigit())
    if digits:
        rep_number = ((int(digits[-2:] or digits) - 1) % 3) + 1
    else:
        digest = hashlib.sha256(lead_id.encode("utf-8")).hexdigest()
        rep_number = (int(digest[:2], 16) % 3) + 1
    return f"Sales Rep {rep_number}"


def fallback_decision(
    request: AddressEvaluationRequest,
    solar: SolarPotential,
    financials: FinancialEstimate,
    scores: LeadScores,
    assigned_rep: str,
) -> DecisionLayerResult:
    if (
        solar.roofSuitability == RoofSuitability.POOR
        or solar.estimatedKwPeak < 3.0
        or scores.profitabilityScore < 0.35
        or request.ownerStatus == OwnerStatus.RENTER
    ):
        return DecisionLayerResult(
            decision=Decision.REJECT,
            resourceLevel=ResourceLevel.LOW_TOUCH,
            nextAction=NextAction.CLOSE_OUT_NOT_A_FIT,
            assignedRep=assigned_rep,
            reasoning=_reasoning_summary(request, solar, financials, scores),
        )

    if (
        scores.leadFitScore >= 0.68
        and scores.profitabilityScore >= 0.58
        and scores.ghostingRiskScore <= 0.58
    ):
        resource_level = (
            ResourceLevel.HIGH_TOUCH
            if scores.leadFitScore >= 0.86
            and scores.profitabilityScore >= 0.78
            and scores.ghostingRiskScore <= 0.34
            else ResourceLevel.MEDIUM_TOUCH
        )
        return DecisionLayerResult(
            decision=Decision.PURSUE,
            resourceLevel=resource_level,
            nextAction=NextAction.GENERATE_OFFER_AND_SCHEDULE_CONSULTATION,
            assignedRep=assigned_rep,
            reasoning=_reasoning_summary(request, solar, financials, scores),
        )

    next_action = (
        NextAction.SEND_FINANCING_INFO_AND_FOLLOW_UP
        if _has_financing_objection(request.objections)
        else NextAction.SEND_EDUCATIONAL_CONTENT_AND_RECHECK
    )
    if request.ownerStatus in {OwnerStatus.UNKNOWN, OwnerStatus.FAMILY_OWNER}:
        next_action = NextAction.QUALIFY_OWNER_STATUS_BEFORE_NEXT_STEP

    return DecisionLayerResult(
        decision=Decision.NURTURE,
        resourceLevel=(
            ResourceLevel.LOW_TOUCH
            if scores.ghostingRiskScore > 0.66
            else ResourceLevel.MEDIUM_TOUCH
        ),
        nextAction=next_action,
        assignedRep=assigned_rep,
        reasoning=_reasoning_summary(request, solar, financials, scores),
    )


def _reasoning_summary(
    request: AddressEvaluationRequest,
    solar: SolarPotential,
    financials: FinancialEstimate,
    scores: LeadScores,
) -> str:
    reasons: list[str] = []

    if solar.roofOrientationScore >= 0.82 and solar.roofPitchScore >= 0.72:
        reasons.append("strong roof potential")
    elif solar.roofSuitability == RoofSuitability.POOR:
        reasons.append("weak roof suitability")
    else:
        reasons.append("usable rooftop solar potential")

    if solar.yearlyEnergyKwh >= 7500:
        reasons.append("good estimated annual energy")
    elif solar.yearlyEnergyKwh >= 4500:
        reasons.append("moderate estimated annual energy")

    if financials.paybackYears <= 9.5:
        reasons.append(f"estimated payback around {financials.paybackYears:.1f} years")

    if request.ownerStatus in {OwnerStatus.OWNER, OwnerStatus.CO_OWNER}:
        reasons.append("owner-occupied property")
    elif request.ownerStatus == OwnerStatus.UNKNOWN:
        reasons.append("ownership still needs confirmation")

    if request.installationTimeline in {
        InstallationTimeline.IMMEDIATELY,
        InstallationTimeline.WITHIN_1_MONTH,
        InstallationTimeline.WITHIN_3_MONTHS,
    }:
        reasons.append("short buying timeline")

    if scores.ghostingRiskScore > 0.60:
        reasons.append("follow-up risk is elevated")

    sentence = ", ".join(reasons[:5])
    return sentence[:1].upper() + sentence[1:] + "."


def _build_gemini_prompt(
    request: AddressEvaluationRequest,
    geocode: GeocodeResult,
    solar: SolarPotential,
    financials: FinancialEstimate,
    scores: LeadScores,
    assigned_rep: str,
) -> str:
    payload = {
        "request": model_to_jsonable(request),
        "geocode": model_to_jsonable(geocode),
        "solarPotential": model_to_jsonable(solar),
        "financials": model_to_jsonable(financials),
        "scores": model_to_jsonable(scores),
        "assignedRep": assigned_rep,
    }
    return (
        "You are Agent 2 of Solar Lead OS, a decision service for German SMB "
        "solar installers. Return strict JSON only. Do not include markdown or "
        "freeform prose. Use the deterministic scores and solar metrics as the "
        "source of truth.\n\n"
        "Decision policy:\n"
        "- PURSUE: strong solar potential and enough lead signal to prepare an offer.\n"
        "- NURTURE: solar looks useful but ownership, timing, or budget needs follow-up.\n"
        "- REJECT: low solar potential, non-owner, or poor economics.\n"
        "- HIGH_TOUCH only for very high fit, high profitability, and low ghosting risk.\n"
        "- Use assignedRep exactly as provided.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _decision_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": [item.value for item in Decision]},
            "resourceLevel": {
                "type": "string",
                "enum": [item.value for item in ResourceLevel],
            },
            "nextAction": {
                "type": "string",
                "enum": [item.value for item in NextAction],
            },
            "assignedRep": {"type": "string"},
            "reasoning": {
                "type": "string",
                "description": "One concise sentence for UI display.",
            },
        },
        "required": [
            "decision",
            "resourceLevel",
            "nextAction",
            "assignedRep",
            "reasoning",
        ],
    }


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    return payload["candidates"][0]["content"]["parts"][0]["text"]


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
        raise ValueError("Gemini decision output must be a JSON object")
    return parsed


def _has_financing_objection(objections: list[str]) -> bool:
    joined = " ".join(objections)
    return any(token in joined for token in ["financ", "budget", "price", "cost"])
