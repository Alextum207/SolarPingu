import re

from app.config import Settings, get_settings
from app.models import (
    AddressEvaluationRequest,
    FinancialEstimate,
    InstallationTimeline,
    LeadScores,
    OwnerStatus,
    RoofSuitability,
    SolarPotential,
)


def calculate_financials(
    solar: SolarPotential,
    battery_interest: bool,
    settings: Settings | None = None,
) -> FinancialEstimate:
    active_settings = settings or get_settings()

    battery_min = active_settings.battery_addon_price_min if battery_interest else 0
    battery_max = active_settings.battery_addon_price_max if battery_interest else 0

    min_price = _round_to_nearest_100(
        (solar.estimatedKwPeak * active_settings.base_price_per_kwp_min) + battery_min
    )
    max_price = _round_to_nearest_100(
        (solar.estimatedKwPeak * active_settings.base_price_per_kwp_max) + battery_max
    )

    self_consumed_kwh = solar.yearlyEnergyKwh * active_settings.self_consumption_ratio
    exported_kwh = solar.yearlyEnergyKwh - self_consumed_kwh
    annual_savings = (
        self_consumed_kwh * active_settings.electricity_price_per_kwh
        + exported_kwh * active_settings.feed_in_tariff_per_kwh
    )

    price_midpoint = (min_price + max_price) / 2
    payback_years = price_midpoint / annual_savings if annual_savings > 0 else 99.0

    return FinancialEstimate(
        estimatedPriceMin=max(min_price, 0),
        estimatedPriceMax=max(max_price, min_price),
        annualSavingsEstimate=max(_round_to_nearest_10(annual_savings), 0),
        paybackYears=round(max(payback_years, 0), 1),
    )


def calculate_scores(
    request: AddressEvaluationRequest,
    solar: SolarPotential,
    financials: FinancialEstimate,
) -> LeadScores:
    solar_score = _solar_potential_score(solar)
    owner_score = _owner_status_score(request.ownerStatus)
    timeline_score = _timeline_score(request.installationTimeline)
    budget_score = _budget_alignment_score(request.budgetRange, financials)
    objection_risk = _objection_risk(request.objections)

    lead_fit = (
        solar_score * 0.45
        + owner_score * 0.18
        + timeline_score * 0.15
        + budget_score * 0.12
        + (0.06 if request.name else 0.02)
        + (0.04 if request.batteryInterest else 0.02)
    )
    lead_fit -= objection_risk * 0.14

    profitability = _profitability_score(solar, financials)

    ghosting_risk = (
        0.12
        + (1 - owner_score) * 0.18
        + (1 - timeline_score) * 0.24
        + (1 - budget_score) * 0.18
        + objection_risk * 0.22
        + (0.06 if not request.name else 0.0)
    )

    return LeadScores(
        leadFitScore=round(_clamp(lead_fit), 2),
        profitabilityScore=round(_clamp(profitability), 2),
        ghostingRiskScore=round(_clamp(ghosting_risk), 2),
    )


def _solar_potential_score(solar: SolarPotential) -> float:
    suitability_score = {
        RoofSuitability.EXCELLENT: 1.0,
        RoofSuitability.GOOD: 0.84,
        RoofSuitability.MODERATE: 0.58,
        RoofSuitability.POOR: 0.22,
    }[solar.roofSuitability]
    size_score = _clamp((solar.estimatedKwPeak - 3.0) / 8.5)
    energy_score = _clamp(solar.yearlyEnergyKwh / 9500)

    return (
        suitability_score * 0.34
        + solar.roofOrientationScore * 0.18
        + solar.roofPitchScore * 0.12
        + size_score * 0.18
        + energy_score * 0.18
    )


def _profitability_score(
    solar: SolarPotential,
    financials: FinancialEstimate,
) -> float:
    kwp_score = _clamp((solar.estimatedKwPeak - 3.5) / 7)
    energy_score = _clamp(solar.yearlyEnergyKwh / 9000)
    savings_score = _clamp(financials.annualSavingsEstimate / 2200)
    payback_score = _clamp((14 - financials.paybackYears) / 8)

    return (
        solar.roofOrientationScore * 0.16
        + solar.roofPitchScore * 0.10
        + kwp_score * 0.22
        + energy_score * 0.18
        + savings_score * 0.14
        + payback_score * 0.20
    )


def _owner_status_score(owner_status: OwnerStatus) -> float:
    return {
        OwnerStatus.OWNER: 1.0,
        OwnerStatus.CO_OWNER: 0.9,
        OwnerStatus.FAMILY_OWNER: 0.68,
        OwnerStatus.PROPERTY_MANAGER: 0.58,
        OwnerStatus.UNKNOWN: 0.55,
        OwnerStatus.RENTER: 0.10,
    }[owner_status]


def _timeline_score(timeline: InstallationTimeline) -> float:
    return {
        InstallationTimeline.IMMEDIATELY: 1.0,
        InstallationTimeline.WITHIN_1_MONTH: 0.96,
        InstallationTimeline.WITHIN_3_MONTHS: 0.88,
        InstallationTimeline.WITHIN_6_MONTHS: 0.66,
        InstallationTimeline.THIS_YEAR: 0.56,
        InstallationTimeline.EXPLORING: 0.34,
        InstallationTimeline.UNKNOWN: 0.50,
    }[timeline]


def _budget_alignment_score(
    budget_range: str,
    financials: FinancialEstimate,
) -> float:
    budget = _parse_budget_range(budget_range)
    if budget is None:
        return 0.56

    budget_min, budget_max = budget
    estimate_min = financials.estimatedPriceMin
    estimate_max = financials.estimatedPriceMax

    if budget_max is None:
        return 0.95 if budget_min <= estimate_max else 0.80

    overlaps_estimate = budget_max >= estimate_min and budget_min <= estimate_max
    if overlaps_estimate:
        return 1.0
    if budget_max >= estimate_min * 0.85:
        return 0.72
    if budget_max >= estimate_min * 0.70:
        return 0.48
    return 0.22


def _parse_budget_range(value: str) -> tuple[int, int | None] | None:
    if value == "unknown":
        return None

    normalized = value.lower().replace(".", "").replace(",", "")
    is_open_ended = any(marker in normalized for marker in ["+", "over", "above", "ab"])
    numbers = [int(match) for match in re.findall(r"\d+", normalized)]
    if not numbers:
        return None

    if "k" in normalized:
        numbers = [number * 1000 if number < 1000 else number for number in numbers]

    if len(numbers) == 1:
        return (numbers[0], None if is_open_ended else numbers[0])
    return (min(numbers[0], numbers[1]), max(numbers[0], numbers[1]))


def _objection_risk(objections: list[str]) -> float:
    if not objections:
        return 0.0

    risk = 0.08
    joined = " ".join(objections)
    if "financ" in joined or "budget" in joined:
        risk += 0.26
    if "too expensive" in joined or "price" in joined or "cost" in joined:
        risk += 0.20
    if "spouse" in joined or "partner" in joined or "decision maker" in joined:
        risk += 0.18
    if "later" in joined or "not urgent" in joined or "research" in joined:
        risk += 0.22
    if "rent" in joined or "landlord" in joined:
        risk += 0.34

    return _clamp(risk)


def _round_to_nearest_100(value: float) -> int:
    return int(round(value / 100) * 100)


def _round_to_nearest_10(value: float) -> int:
    return int(round(value / 10) * 10)


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))
