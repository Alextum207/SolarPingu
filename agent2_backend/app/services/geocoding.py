import hashlib
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import GeocodeDataSource, GeocodeResult


MOCK_LOCATION_HINTS = [
    {
        "tokens": ("schnittelberg",),
        "formatted": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
        "latitude": 50.160936,
        "longitude": 8.486981,
        "place_id": "mock-place-schnittelberg-14",
    },
    {
        "tokens": ("frankfurt", "60311"),
        "formatted": "60311 Frankfurt am Main, Germany",
        "latitude": 50.1109,
        "longitude": 8.6821,
        "place_id": "mock-place-frankfurt",
    },
]


class GeocodingService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def geocode_address(self, address: str) -> GeocodeResult:
        if self.settings.use_mock_geocoding or not self.settings.geocoding_api_key:
            return self._mock_geocode(address, "mock geocoding enabled or API key missing")

        try:
            timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.settings.google_geocoding_url,
                    params={
                        "address": address,
                        "key": self.settings.geocoding_api_key,
                        "region": "de",
                    },
                )
                response.raise_for_status()
                return self._parse_geocoding_response(address, response.json())
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            return self._mock_geocode(address, "Google Geocoding API request failed")

    def _parse_geocoding_response(
        self,
        input_address: str,
        payload: dict[str, Any],
    ) -> GeocodeResult:
        if payload.get("status") != "OK" or not payload.get("results"):
            status = payload.get("status") or "UNKNOWN_STATUS"
            error_message = payload.get("error_message")
            reason = f"Google Geocoding API returned {status}"
            if error_message:
                reason = f"{reason}: {error_message}"
            return self._mock_geocode(input_address, reason)

        result = payload["results"][0]
        location = result["geometry"]["location"]

        return GeocodeResult(
            inputAddress=input_address,
            formattedAddress=result.get("formatted_address") or input_address,
            latitude=round(float(location["lat"]), 6),
            longitude=round(float(location["lng"]), 6),
            placeId=result.get("place_id"),
            dataSource=GeocodeDataSource.GOOGLE_GEOCODING_API,
        )

    def _mock_geocode(self, address: str, reason: str | None = None) -> GeocodeResult:
        normalized_address = address.strip()
        lowered = normalized_address.lower()
        for hint in MOCK_LOCATION_HINTS:
            if any(token in lowered for token in hint["tokens"]):
                return GeocodeResult(
                    inputAddress=normalized_address,
                    formattedAddress=normalized_address or str(hint["formatted"]),
                    latitude=float(hint["latitude"]),
                    longitude=float(hint["longitude"]),
                    placeId=str(hint["place_id"]),
                    dataSource=GeocodeDataSource.MOCK,
                    fallbackReason=reason,
                )

        digest = hashlib.sha256(lowered.encode("utf-8")).hexdigest()
        seed = int(digest[:12], 16)

        latitude = 47.4 + ((seed % 7600) / 1000)
        longitude = 6.0 + (((seed // 7600) % 8200) / 1000)

        return GeocodeResult(
            inputAddress=normalized_address,
            formattedAddress=normalized_address,
            latitude=round(latitude, 6),
            longitude=round(longitude, 6),
            placeId=f"mock-place-{digest[:10]}",
            dataSource=GeocodeDataSource.MOCK,
            fallbackReason=reason,
        )
