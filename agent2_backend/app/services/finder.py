import hashlib
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings, get_settings
from app.models import (
    AddressEvaluationRequest,
    Agent1DeliveryResult,
    Agent1DeliveryStatus,
    BusinessCandidate,
    BusinessSearchRequest,
    Decision,
    FinderLead,
    FinderRunResponse,
    FinderSolarSummary,
    FinderTraceEvent,
    ProjectDecision,
    VisionAnalysis,
)
from app.services.agent1 import Agent1WebhookService
from app.services.evaluation import EvaluationService
from app.services.places import PlacesService
from app.services.solar import SolarService
from app.services.vision import FeatherlessVisionService


DEFAULT_BUSINESS_CATEGORIES = [
    "Autohaus",
    "Logistik",
    "Lagerhalle",
    "Produktion",
    "Großhandel",
    "Baumarkt",
    "Supermarkt",
    "Fitnessstudio",
    "Möbelhaus",
    "Gewerbepark",
]

TraceCallback = Callable[[FinderTraceEvent], Awaitable[None]]


class BusinessFinderService:
    def __init__(
        self,
        settings: Settings | None = None,
        places_service: PlacesService | None = None,
        evaluation_service: EvaluationService | None = None,
        solar_service: SolarService | None = None,
        vision_service: FeatherlessVisionService | None = None,
        agent1_service: Agent1WebhookService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.places_service = places_service or PlacesService(self.settings)
        self.evaluation_service = evaluation_service or EvaluationService(self.settings)
        self.solar_service = solar_service or self.evaluation_service.solar_service
        self.vision_service = vision_service or FeatherlessVisionService(self.settings)
        self.agent1_service = agent1_service or Agent1WebhookService(self.settings)

    async def run(
        self,
        request: BusinessSearchRequest,
        trace_callback: TraceCallback | None = None,
    ) -> FinderRunResponse:
        run_id = _run_id_from_city(request.city)
        trace: list[FinderTraceEvent] = []
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Finder gestartet",
                tool="BusinessFinderService",
                status="RUNNING",
                thought=(
                    "Ich starte mit der Stadt und nutze die vorkonfigurierten "
                    "B2B-Kategorien, weil die Suche im Frontend bewusst schlank ist."
                ),
                detail=f"runId={run_id}, city={request.city}",
            ),
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Businesses suchen",
                tool="Google Places Text Search",
                status="RUNNING",
                thought=(
                    "Ich suche nach gewerblichen Dachflächen, die typischerweise "
                    "groß genug für PV sind."
                ),
                detail=", ".join(DEFAULT_BUSINESS_CATEGORIES),
            ),
        )
        candidates = await self.places_service.find_businesses(
            city=request.city,
            categories=DEFAULT_BUSINESS_CATEGORIES,
            max_results=self.settings.finder_max_places_per_run,
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Businesses gefunden",
                tool="PlacesService",
                status="DONE",
                thought=(
                    "Ich habe die Orte dedupliziert und prüfe sie jetzt einzeln "
                    "durch Agent 2 und Vision."
                ),
                detail=f"{len(candidates)} Kandidaten",
            )
        )

        leads: list[FinderLead] = []
        for index, candidate in enumerate(candidates, start=1):
            leads.append(
                await self._process_candidate(
                    candidate,
                    trace,
                    trace_callback,
                    index,
                    len(candidates),
                )
            )

        qualified_count = sum(1 for lead in leads if lead.qualified)
        sent_count = sum(1 for lead in leads if lead.sentToAgent1)
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Finder abgeschlossen",
                tool="BusinessFinderService",
                status="DONE",
                thought=(
                    "Ich habe alle Kandidaten bewertet und nur qualifizierte Leads "
                    "fuer Agent 1 vorbereitet."
                ),
                detail=(
                    f"{qualified_count} qualifiziert, "
                    f"{sent_count} an Agent 1 gesendet"
                ),
            )
        )
        return FinderRunResponse(
            runId=run_id,
            city=request.city,
            discoveredCount=len(candidates),
            qualifiedCount=qualified_count,
            sentToAgent1Count=sent_count,
            trace=trace,
            leads=leads,
        )

    async def _process_candidate(
        self,
        candidate: BusinessCandidate,
        trace: list[FinderTraceEvent],
        trace_callback: TraceCallback | None,
        index: int,
        total: int,
    ) -> FinderLead:
        lead_id = _lead_id_for_candidate(candidate)
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step=f"Kandidat {index}/{total}",
                tool="BusinessFinderService",
                status="RUNNING",
                thought=(
                    "Ich nehme diesen Treffer als potenziellen Gewerbe-Lead und "
                    "pruefe zuerst Adresse und Solarpotenzial."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=f"{candidate.category}, leadId={lead_id}",
            )
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Agent 2 prueft Adresse",
                tool="Geocoding + Google Solar + Maps Static",
                status="RUNNING",
                thought=(
                    "Ich lasse Agent 2 Standort, Dachbild, Solarleistung und "
                    "wirtschaftliche Scores berechnen."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
            )
        )
        decision = await self.evaluation_service.evaluate(
            AddressEvaluationRequest(
                leadId=lead_id,
                address=candidate.address,
                name=candidate.businessName,
                ownerStatus="property_manager",
            )
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Agent 2 Ergebnis",
                tool="Agent 2 evaluate",
                status="DONE",
                thought=(
                    "Jetzt kenne ich die technische und wirtschaftliche Basis und "
                    "kann das Dachbild visuell gegenpruefen."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=(
                    f"{decision.decision.value}, {decision.estimatedKwPeak:.1f} kWp, "
                    f"Profit {decision.profitabilityScore:.2f}"
                ),
            )
        )
        roof_image = self.solar_service.get_cached_image_for_url(decision.roofImageUrl)
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Dachbild analysieren",
                tool="Featherless Vision",
                status="RUNNING",
                thought=(
                    "Ich pruefe das Satellitenbild auf sichtbare Dachform, Blocker "
                    "und grobes Solarpotenzial."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=decision.roofImageUrl,
            )
        )
        vision = await self.vision_service.analyze_roof_image(
            image=roof_image,
            business=candidate,
            decision=decision,
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Vision Ergebnis",
                tool="Featherless Vision",
                status="DONE" if not vision.warning else "WARN",
                thought=(
                    "Ich kombiniere die visuelle Einschaetzung mit den Agent-2-Scores, "
                    "damit kein Lead nur wegen eines einzelnen Signals durchkommt."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=(
                    vision.warning
                    or f"Vision {vision.visualSolarPotentialScore:.2f}, {vision.roofType}"
                ),
            )
        )
        qualified, reason = qualifies_finder_lead(
            decision=decision.decision,
            estimated_kw_peak=decision.estimatedKwPeak,
            profitability_score=decision.profitabilityScore,
            vision=vision,
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Lead qualifizieren",
                tool="Qualification Rules",
                status="DONE",
                thought=(
                    "Ich entscheide anhand von Agent-2-Entscheidung, kWp, Profit "
                    "und Vision-Score, ob dieser Lead weitergegeben wird."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=reason,
            )
        )

        solar_summary = FinderSolarSummary(
            estimatedKwPeak=decision.estimatedKwPeak,
            yearlyEnergyKwh=decision.yearlyEnergyKwh,
            panelCount=decision.panelCount,
            profitabilityScore=decision.profitabilityScore,
            decision=decision.decision,
        )
        delivery = (
            await self.agent1_service.send_lead(
                payload=_agent1_payload(
                    lead_id=lead_id,
                    candidate=candidate,
                    solar=solar_summary,
                    vision=vision,
                ),
                idempotency_key=lead_id,
            )
            if qualified
            else Agent1DeliveryResult(
                status=Agent1DeliveryStatus.SKIPPED,
                sent=False,
                warning="Lead did not meet qualification threshold",
            )
        )
        await _add_trace(
            trace,
            trace_callback,
            _trace(
                step="Agent 1 Uebergabe",
                tool="Agent1WebhookService",
                status=delivery.status.value,
                thought=(
                    "Wenn der Lead qualifiziert ist, schicke ich nur oeffentliche "
                    "Infos plus Solar- und Vision-Summary an Agent 1."
                ),
                address=candidate.address,
                business_name=candidate.businessName,
                detail=(
                    delivery.warning
                    or ("Webhook erfolgreich" if delivery.sent else "Nicht gesendet")
                ),
            )
        )

        return FinderLead(
            leadId=lead_id,
            source=candidate.source,
            businessName=candidate.businessName,
            category=candidate.category,
            address=candidate.address,
            phone=candidate.phone,
            website=candidate.website,
            googleMapsUrl=candidate.googleMapsUrl,
            rating=candidate.rating,
            roofImageUrl=decision.roofImageUrl,
            solar=solar_summary,
            vision=vision,
            qualified=qualified,
            qualificationReason=reason,
            sentToAgent1=delivery.sent,
            agent1Status=delivery.status,
            agent1Warning=delivery.warning,
        )


def qualifies_finder_lead(
    decision: Decision,
    estimated_kw_peak: float,
    profitability_score: float,
    vision: VisionAnalysis,
) -> tuple[bool, str]:
    if decision == Decision.REJECT:
        return False, "Agent 2 rejected this business"
    if estimated_kw_peak < 8:
        return False, "Estimated PV size is below 8 kWp"
    if profitability_score < 0.55:
        return False, "Profitability score is below 0.55"
    if vision.warning:
        return True, "Qualified by Agent 2; vision analysis unavailable"
    if vision.visualSolarPotentialScore < 0.60:
        return False, "Vision score is below 0.60"
    return True, "Qualified by Agent 2 and Featherless vision"


def _agent1_payload(
    lead_id: str,
    candidate: BusinessCandidate,
    solar: FinderSolarSummary,
    vision: VisionAnalysis,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "leadId": lead_id,
        "source": candidate.source.value,
        "businessName": candidate.businessName,
        "category": candidate.category,
        "address": candidate.address,
        "phone": candidate.phone,
        "website": candidate.website,
        "googleMapsUrl": candidate.googleMapsUrl,
        "rating": candidate.rating,
        "solar": {
            "estimatedKwPeak": solar.estimatedKwPeak,
            "yearlyEnergyKwh": solar.yearlyEnergyKwh,
            "panelCount": solar.panelCount,
            "profitabilityScore": solar.profitabilityScore,
            "decision": solar.decision.value,
        },
        "vision": {
            "visualSolarPotentialScore": vision.visualSolarPotentialScore,
            "roofType": vision.roofType,
            "blockers": vision.blockers,
        },
        "publicInfoOnly": True,
    }
    if vision.warning:
        payload["visionWarning"] = vision.warning
    return payload


def _lead_id_for_candidate(candidate: BusinessCandidate) -> str:
    source = candidate.placeId or f"{candidate.businessName}:{candidate.address}"
    digest = hashlib.sha256(source.lower().encode("utf-8")).hexdigest()
    return f"FINDER-{digest[:10].upper()}"


def _run_id_from_city(city: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    digest = hashlib.sha256(f"{city}:{timestamp}".lower().encode("utf-8")).hexdigest()
    return f"RUN-{timestamp}-{digest[:6].upper()}"


def _trace(
    step: str,
    tool: str,
    status: str,
    thought: str,
    address: str | None = None,
    business_name: str | None = None,
    detail: str | None = None,
) -> FinderTraceEvent:
    return FinderTraceEvent(
        step=step,
        tool=tool,
        status=status,
        thought=thought,
        address=address,
        businessName=business_name,
        detail=detail,
    )


async def _add_trace(
    trace: list[FinderTraceEvent],
    trace_callback: TraceCallback | None,
    event: FinderTraceEvent,
) -> None:
    trace.append(event)
    if trace_callback is not None:
        await trace_callback(event)
