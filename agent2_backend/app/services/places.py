import hashlib
from math import ceil
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.config import Settings, get_settings
from app.models import BusinessCandidate, BusinessLeadSource


PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.rating",
        "places.location",
        "places.types",
    ]
)


class PlacesService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def find_businesses(
        self,
        city: str,
        categories: list[str],
        max_results: int,
    ) -> list[BusinessCandidate]:
        if self.settings.use_mock_places or not self.settings.google_places_api_key:
            return self._mock_businesses(city=city, categories=categories, limit=max_results)

        per_category_limit = max(1, min(20, ceil(max_results / max(len(categories), 1))))
        candidates: list[BusinessCandidate] = []
        timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for category in categories:
                if len(candidates) >= max_results:
                    break
                try:
                    response = await client.post(
                        self.settings.google_places_text_search_url,
                        headers={
                            "Content-Type": "application/json",
                            "X-Goog-Api-Key": self.settings.google_places_api_key,
                            "X-Goog-FieldMask": PLACES_FIELD_MASK,
                        },
                        json={
                            "textQuery": f"{category} in {city}",
                            "languageCode": "de",
                            "regionCode": "DE",
                            "maxResultCount": per_category_limit,
                        },
                    )
                    response.raise_for_status()
                    candidates.extend(
                        _parse_places_response(
                            payload=response.json(),
                            category=category,
                        )
                    )
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    continue

        deduped = dedupe_business_candidates(candidates)
        if deduped:
            return deduped[:max_results]
        return self._mock_businesses(city=city, categories=categories, limit=max_results)

    def _mock_businesses(
        self,
        city: str,
        categories: list[str],
        limit: int,
    ) -> list[BusinessCandidate]:
        street_names = [
            "Industriestrasse",
            "Hanauer Landstrasse",
            "Gewerbeallee",
            "Solarparkweg",
            "Werkstrasse",
            "Logistikring",
        ]
        candidates: list[BusinessCandidate] = []
        for index, category in enumerate(categories):
            if len(candidates) >= limit:
                break
            street = street_names[index % len(street_names)]
            number = 10 + (index * 7)
            name = f"{category} {city}"
            address = f"{name}, {street} {number}, {city}, Germany"
            digest = hashlib.sha256(address.lower().encode("utf-8")).hexdigest()
            candidates.append(
                BusinessCandidate(
                    placeId=f"mock-place-{digest[:10]}",
                    businessName=name,
                    category=category,
                    address=address,
                    phone=f"+49 69 000{index:03d}",
                    website=f"https://example.com/{quote_plus(category.lower())}",
                    googleMapsUrl=(
                        "https://www.google.com/maps/search/?api=1&query="
                        f"{quote_plus(address)}"
                    ),
                    rating=round(3.9 + ((index % 8) * 0.13), 1),
                    source=BusinessLeadSource.MOCK,
                )
            )
        return candidates


def dedupe_business_candidates(
    candidates: list[BusinessCandidate],
) -> list[BusinessCandidate]:
    seen: set[str] = set()
    deduped: list[BusinessCandidate] = []
    for candidate in candidates:
        key = candidate.placeId.strip().lower()
        if not key:
            key = _normalize_address(candidate.address)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _parse_places_response(
    payload: dict[str, Any],
    category: str,
) -> list[BusinessCandidate]:
    candidates: list[BusinessCandidate] = []
    for place in payload.get("places") or []:
        name = _display_name_text(place.get("displayName"))
        address = str(place.get("formattedAddress") or "").strip()
        place_id = str(place.get("id") or "").strip()
        if not name or not address or not place_id:
            continue

        location = place.get("location") or {}
        candidates.append(
            BusinessCandidate(
                placeId=place_id,
                businessName=name,
                category=category,
                address=address,
                phone=_optional_string(place.get("nationalPhoneNumber")),
                website=_optional_string(place.get("websiteUri")),
                googleMapsUrl=_optional_string(place.get("googleMapsUri")),
                rating=_optional_float(place.get("rating")),
                latitude=_optional_float(location.get("latitude")),
                longitude=_optional_float(location.get("longitude")),
                source=BusinessLeadSource.GOOGLE_PLACES,
            )
        )
    return candidates


def _display_name_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_address(address: str) -> str:
    return " ".join(address.lower().replace(",", " ").split())
