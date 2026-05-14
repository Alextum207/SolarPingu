from __future__ import annotations

from app.models import ProfitabilityDecision, SolarLeadIntake


BUDGET_MIN = {
    "under_10000": 8000,
    "10000-15000": 10000,
    "15000-20000": 15000,
    "20000-30000": 20000,
    "over_30000": 30000,
    "unknown": 0,
}

BUDGET_MAX = {
    "under_10000": 10000,
    "10000-15000": 15000,
    "15000-20000": 20000,
    "20000-30000": 30000,
    "over_30000": 42000,
    "unknown": 0,
}

TIMELINE_SCORE = {
    "immediate": 18,
    "within_3_months": 18,
    "within_6_months": 14,
    "within_12_months": 8,
    "exploring": 3,
}


def evaluate_profitability(
    lead: SolarLeadIntake,
    solar_enrichment: dict,
) -> ProfitabilityDecision:
    potential = solar_enrichment.get("solar_potential", {})
    estimated_kwp = float(potential.get("estimated_kwp") or 7.5)
    battery_revenue = 6500 if lead.battery_interest else 0
    wallbox_revenue = 1600 if lead.wallbox_interest else 0
    estimated_price_min = int(max(12000, estimated_kwp * 1450 + battery_revenue + wallbox_revenue))
    estimated_price_max = int(estimated_price_min * 1.18)
    estimated_margin = int(estimated_price_min * 0.22)

    score = 0
    reasons: list[str] = []
    disqualifiers: list[str] = []

    if lead.owner_status == "owner":
        score += 24
        reasons.append("Eigentümerstatus bestätigt.")
    elif lead.owner_status == "renter":
        disqualifiers.append("Mieter ohne Entscheidungsgewalt.")
    else:
        score += 4
        reasons.append("Eigentümerstatus ist noch unklar.")

    score += TIMELINE_SCORE[lead.timeline]
    if lead.timeline in {"immediate", "within_3_months", "within_6_months"}:
        reasons.append("Zeitfenster zeigt kurzfristige Kaufabsicht.")

    budget_max = BUDGET_MAX[lead.budget_range]
    if lead.budget_range == "unknown":
        score -= 8
        reasons.append("Budget fehlt und muss vor Angebot geklärt werden.")
    elif budget_max >= estimated_price_min * 0.75:
        score += 18
        reasons.append("Budget ist plausibel für die geschätzte Projektgröße.")
    else:
        score -= 12
        disqualifiers.append("Budget liegt deutlich unter der erwarteten Projektgröße.")

    if estimated_kwp >= 7:
        score += 16
        reasons.append("Dachpotenzial ist wirtschaftlich attraktiv.")
    elif estimated_kwp >= 4:
        score += 8
        reasons.append("Dachpotenzial ist solide, aber nicht groß.")
    else:
        disqualifiers.append("Dachpotenzial zu klein für profitablen Außendiensttermin.")

    if lead.roof_type in {"pitched", "flat"}:
        score += 8
        reasons.append("Dachtyp ist verwertbar.")
    else:
        score -= 5

    if lead.battery_interest:
        score += 8
        reasons.append("Batterieinteresse erhöht Projektwert und Marge.")
    if lead.wallbox_interest:
        score += 4
        reasons.append("Wallboxinteresse erhöht Cross-Sell-Potenzial.")

    if lead.decision_maker.strip().lower() in {"unknown", "unbekannt"}:
        score -= 8
    else:
        score += 4

    score = max(0, min(100, score))
    hard_reject = any(
        item.startswith("Mieter") or item.startswith("Dachpotenzial zu klein")
        for item in disqualifiers
    )

    if hard_reject or score < 45:
        decision = "REJECT"
        next_action = "polite_reject"
        resource_level = "low_touch"
    elif score >= 70 and lead.owner_status == "owner":
        decision = "PURSUE"
        next_action = "send_booking_link"
        resource_level = "high_touch" if score >= 84 else "medium_touch"
    else:
        decision = "NURTURE"
        next_action = "request_missing_info"
        resource_level = "low_touch"

    return ProfitabilityDecision(
        profitable=decision == "PURSUE",
        decision=decision,
        score=score,
        resource_level=resource_level,
        estimated_kwp=estimated_kwp,
        estimated_price_min=estimated_price_min,
        estimated_price_max=estimated_price_max,
        estimated_margin=estimated_margin,
        payback_years=round(estimated_price_min / max(900, estimated_kwp * 210), 1),
        reasons=reasons[:6],
        disqualifiers=disqualifiers,
        next_action=next_action,
    )
