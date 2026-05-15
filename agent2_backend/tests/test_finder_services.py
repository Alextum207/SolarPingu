import asyncio
import json

import httpx

from app.config import Settings
from app.models import (
    Agent1DeliveryStatus,
    BusinessCandidate,
    BusinessLeadSource,
    Decision,
    VisionAnalysis,
)
from app.services.agent1 import Agent1WebhookService
from app.services.finder import qualifies_finder_lead
from app.services.places import dedupe_business_candidates
from app.services.vision import parse_featherless_vision_text


def test_places_dedupe_uses_place_id_first() -> None:
    candidates = [
        BusinessCandidate(
            placeId="place-1",
            businessName="Autohaus A",
            category="Autohaus",
            address="Industriestrasse 1, Frankfurt",
            source=BusinessLeadSource.GOOGLE_PLACES,
        ),
        BusinessCandidate(
            placeId="place-1",
            businessName="Autohaus A Duplicate",
            category="Autohaus",
            address="Industriestrasse 1, Frankfurt",
            source=BusinessLeadSource.GOOGLE_PLACES,
        ),
        BusinessCandidate(
            placeId="place-2",
            businessName="Logistik B",
            category="Logistik",
            address="Werkstrasse 2, Frankfurt",
            source=BusinessLeadSource.GOOGLE_PLACES,
        ),
    ]

    deduped = dedupe_business_candidates(candidates)

    assert [candidate.placeId for candidate in deduped] == ["place-1", "place-2"]


def test_qualification_uses_agent2_and_vision_thresholds() -> None:
    vision = VisionAnalysis(
        visualSolarPotentialScore=0.78,
        roofType="flat_commercial_roof",
        blockers=[],
        confidence=0.8,
    )

    qualified, reason = qualifies_finder_lead(
        decision=Decision.PURSUE,
        estimated_kw_peak=9.2,
        profitability_score=0.61,
        vision=vision,
    )

    assert qualified is True
    assert "Featherless" in reason


def test_qualification_falls_back_when_vision_failed() -> None:
    vision = VisionAnalysis(
        visualSolarPotentialScore=0,
        roofType="unknown",
        blockers=[],
        confidence=0,
        warning="Featherless vision request failed",
    )

    qualified, reason = qualifies_finder_lead(
        decision=Decision.NURTURE,
        estimated_kw_peak=10,
        profitability_score=0.59,
        vision=vision,
    )

    assert qualified is True
    assert "vision analysis unavailable" in reason


def test_featherless_vision_parser_handles_json_fences() -> None:
    result = parse_featherless_vision_text(
        """```json
        {
          "visualSolarPotentialScore": "78%",
          "roofType": "flat_commercial_roof",
          "blockers": ["small shadows"],
          "confidence": 0.82
        }
        ```"""
    )

    assert result.visualSolarPotentialScore == 0.78
    assert result.roofType == "flat_commercial_roof"
    assert result.blockers == ["small shadows"]


def test_agent1_webhook_success_uses_idempotency_key() -> None:
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["idempotency"] = request.headers.get("Idempotency-Key")
        body = json.loads(request.content.decode("utf-8"))
        assert body["leadId"] == "FINDER-123"
        return httpx.Response(202)

    transport = httpx.MockTransport(handler)
    service = Agent1WebhookService(
        Settings(
            agent1_webhook_url="https://agent1.local/leads",
            agent1_webhook_max_attempts=1,
        ),
        transport=transport,
    )

    result = asyncio.run(
        service.send_lead({"leadId": "FINDER-123"}, idempotency_key="FINDER-123")
    )

    assert result.status == Agent1DeliveryStatus.SENT
    assert result.sent is True
    assert seen_headers["idempotency"] == "FINDER-123"
