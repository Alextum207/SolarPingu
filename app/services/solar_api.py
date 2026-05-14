from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.models import SolarLeadIntake


GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
SOLAR_URL = "https://solar.googleapis.com/v1/buildingInsights:findClosest"


def _fallback(lead: SolarLeadIntake, reason: str) -> dict[str, Any]:
    roof_bonus = 1.0 if lead.roof_type == "pitched" else 0.82 if lead.roof_type == "flat" else 0.72
    battery_bonus = 1.12 if lead.battery_interest else 1.0
    estimated_kwp = round(8.5 * roof_bonus * battery_bonus, 1)
    return {
        "source": "deterministic_fallback",
        "warning": reason,
        "coordinates": None,
        "solar_potential": {
            "estimated_kwp": estimated_kwp,
            "yearly_energy_kwh": int(estimated_kwp * 930),
            "roof_area_m2": int(estimated_kwp * 6.2),
            "max_sunshine_hours_per_year": 980,
            "confidence": 0.62,
        },
    }


async def enrich_solar_potential(lead: SolarLeadIntake) -> dict[str, Any]:
    if not settings.google_solar_api_key:
        return _fallback(lead, "GOOGLE_SOLAR_API_KEY is not configured.")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            geocode = await client.get(
                GEOCODE_URL,
                params={"address": lead.address, "key": settings.google_solar_api_key},
            )
            geocode.raise_for_status()
            geocode_data = geocode.json()
            location = geocode_data["results"][0]["geometry"]["location"]
            solar = await client.get(
                SOLAR_URL,
                params={
                    "location.latitude": location["lat"],
                    "location.longitude": location["lng"],
                    "requiredQuality": "LOW",
                    "key": settings.google_solar_api_key,
                },
            )
            solar.raise_for_status()
            solar_data = solar.json()
    except Exception as exc:
        return _fallback(lead, f"Solar API fallback used: {exc}")

    potential = solar_data.get("solarPotential", {})
    max_array = potential.get("maxArrayPanelsCount") or 24
    panel_capacity_watts = potential.get("panelCapacityWatts") or 420
    estimated_kwp = round(max_array * panel_capacity_watts / 1000, 1)
    yearly_energy = potential.get("maxArrayAreaMeters2")
    if yearly_energy:
        yearly_energy = int(float(yearly_energy) * 150)
    else:
        yearly_energy = int(estimated_kwp * 950)

    return {
        "source": "google_solar_api",
        "coordinates": location,
        "raw_quality": solar_data.get("imageryQuality"),
        "solar_potential": {
            "estimated_kwp": estimated_kwp,
            "yearly_energy_kwh": yearly_energy,
            "roof_area_m2": potential.get("maxArrayAreaMeters2"),
            "max_sunshine_hours_per_year": potential.get("maxSunshineHoursPerYear"),
            "confidence": 0.86,
        },
    }
