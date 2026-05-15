import hashlib

from app.config import Settings, get_settings
from app.models import AddressEvaluationRequest, ProjectDecision
from app.services.decision import DecisionService
from app.services.geocoding import GeocodingService
from app.services.scoring import calculate_financials, calculate_scores
from app.services.solar import SolarService


class EvaluationService:
    def __init__(
        self,
        settings: Settings | None = None,
        geocoding_service: GeocodingService | None = None,
        solar_service: SolarService | None = None,
        decision_service: DecisionService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.geocoding_service = geocoding_service or GeocodingService(self.settings)
        self.solar_service = solar_service or SolarService(self.settings)
        self.decision_service = decision_service or DecisionService(self.settings)

    async def evaluate(self, request: AddressEvaluationRequest) -> ProjectDecision:
        lead_id = request.leadId or lead_id_from_address(request.address)
        request_with_id = request.model_copy(update={"leadId": lead_id})

        geocode = await self.geocoding_service.geocode_address(request.address)
        solar = await self.solar_service.fetch_solar_data(geocode)
        financials = calculate_financials(
            solar=solar,
            battery_interest=request.batteryInterest,
            settings=self.settings,
        )
        visualization = await self.solar_service.fetch_roof_visualization(
            geocode=geocode,
            lead_id=lead_id,
        )
        scores = calculate_scores(
            request=request_with_id,
            solar=solar,
            financials=financials,
        )
        decision = await self.decision_service.decide(
            request=request_with_id,
            geocode=geocode,
            solar=solar,
            financials=financials,
            scores=scores,
            lead_id=lead_id,
        )

        return ProjectDecision(
            leadId=lead_id,
            inputAddress=geocode.inputAddress,
            formattedAddress=geocode.formattedAddress,
            latitude=geocode.latitude,
            longitude=geocode.longitude,
            placeId=geocode.placeId,
            geocodeSource=geocode.dataSource,
            solarSource=solar.dataSource,
            fallbackWarning=_fallback_warning(
                geocode.fallbackReason,
                solar.fallbackReason,
            ),
            roofImageUrl=visualization.roofImageUrl,
            roofImageSource=visualization.roofImageSource,
            imageryDate=visualization.imageryDate,
            imageWarning=visualization.imageWarning,
            panelCount=solar.maxPanels,
            panelCapacityWatts=solar.panelCapacityWatts,
            estimatedKwPeak=solar.estimatedKwPeak,
            yearlyEnergyKwh=solar.yearlyEnergyKwh,
            roofOrientationScore=solar.roofOrientationScore,
            roofPitchScore=solar.roofPitchScore,
            annualSavingsEstimate=financials.annualSavingsEstimate,
            paybackYears=financials.paybackYears,
            estimatedPriceMin=financials.estimatedPriceMin,
            estimatedPriceMax=financials.estimatedPriceMax,
            leadFitScore=scores.leadFitScore,
            profitabilityScore=scores.profitabilityScore,
            ghostingRiskScore=scores.ghostingRiskScore,
            decision=decision.decision,
            resourceLevel=decision.resourceLevel,
            nextAction=decision.nextAction,
            assignedRep=decision.assignedRep,
            reasoning=decision.reasoning,
        )


def lead_id_from_address(address: str) -> str:
    digest = hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()
    return f"ADDR-{digest[:6].upper()}"


def _fallback_warning(
    geocode_reason: str | None,
    solar_reason: str | None,
) -> str | None:
    reasons = [reason for reason in [geocode_reason, solar_reason] if reason]
    if not reasons:
        return None
    return " / ".join(reasons)
