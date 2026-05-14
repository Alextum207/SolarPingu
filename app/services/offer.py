from __future__ import annotations

from uuid import uuid4

from app.models import OfferDraft, OfferPriceRange, ProfitabilityDecision, SolarLeadIntake
from app.services import gemini


async def create_offer(
    lead: SolarLeadIntake,
    profitability: ProfitabilityDecision,
    solar_enrichment: dict,
) -> OfferDraft:
    fallback = {
        "offer_id": f"OFF-{uuid4().hex[:8].upper()}",
        "lead_id": lead.lead_id,
        "package_name": "Solar Lead OS Smart PV Paket",
        "system_size_kwp": profitability.estimated_kwp,
        "includes_battery": lead.battery_interest,
        "price_range": {
            "min": profitability.estimated_price_min,
            "max": profitability.estimated_price_max,
            "currency": "EUR",
        },
        "value_pitch": [
            "Senkung der Stromkosten durch Eigenverbrauch.",
            "Strukturierte Planung mit Speicheroptionen und schneller Vorprüfung.",
            "Wirtschaftlich priorisiertes Angebot statt generischer Beratung.",
        ],
        "assumptions": [
            "Preisrange basiert auf Formularangaben und Solar-Potenzial.",
            "Finale Statik, Zählerschrank und Verschattung werden im Termin geprüft.",
        ],
        "next_steps": [
            "Termin buchen",
            "Dach- und Verbrauchsdaten validieren",
            "Finales Angebot erstellen",
        ],
    }
    result = await gemini.generate_structured_json(
        system_prompt=(
            "You create concise German solar offer drafts. Return strict JSON matching "
            "OfferDraft. Keep it concrete and sales-ready, not fluffy."
        ),
        payload={
            "lead": lead.model_dump(),
            "profitability": profitability.model_dump(),
            "solar_enrichment": solar_enrichment,
            "fallback_shape": fallback,
        },
        temperature=0.2,
        fallback=fallback,
    )
    result.setdefault("offer_id", fallback["offer_id"])
    result.setdefault("lead_id", lead.lead_id)
    result.setdefault("price_range", fallback["price_range"])
    return OfferDraft(
        offer_id=str(result["offer_id"]),
        lead_id=str(result["lead_id"]),
        package_name=str(result.get("package_name") or fallback["package_name"]),
        system_size_kwp=float(result.get("system_size_kwp") or profitability.estimated_kwp),
        includes_battery=bool(result.get("includes_battery", lead.battery_interest)),
        price_range=OfferPriceRange.model_validate(result["price_range"]),
        value_pitch=[str(x) for x in result.get("value_pitch", fallback["value_pitch"])],
        assumptions=[str(x) for x in result.get("assumptions", fallback["assumptions"])],
        next_steps=[str(x) for x in result.get("next_steps", fallback["next_steps"])],
    )
