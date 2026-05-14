from __future__ import annotations

from app import db
from app.config import settings
from app.models import HubHandoffPayload, OfferDraft, ProfitabilityDecision, SolarLeadIntake


def demo_url(lead_id: str) -> str:
    return f"{settings.public_base_url}/demo/{lead_id}"


def create_handoff(
    lead: SolarLeadIntake,
    profitability: ProfitabilityDecision,
    solar_enrichment: dict,
    offer: OfferDraft,
    offer_pdf_url: str | None = None,
) -> HubHandoffPayload:
    return HubHandoffPayload(
        lead=lead,
        profitability=profitability,
        solar_enrichment=solar_enrichment,
        offer=offer,
        offer_pdf_url=offer_pdf_url,
        demo_url=demo_url(lead.lead_id or ""),
        created_at=db.now_iso(),
    )
