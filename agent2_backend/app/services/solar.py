import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import (
    GeocodeResult,
    RoofImageSource,
    RoofSuitability,
    RoofVisualization,
    SolarDataSource,
    SolarPotential,
)


@dataclass
class CachedImage:
    content: bytes
    media_type: str = "image/png"


IMAGE_CACHE: dict[str, CachedImage] = {}


class SolarService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def fetch_solar_data(self, geocode: GeocodeResult) -> SolarPotential:
        if self.settings.use_mock_solar or not self.settings.google_solar_api_key:
            return self._mock_solar_data(
                geocode,
                "mock solar enabled or API key missing",
            )

        try:
            timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.settings.google_solar_url,
                    params={
                        "location.latitude": geocode.latitude,
                        "location.longitude": geocode.longitude,
                        "requiredQuality": "MEDIUM",
                        "key": self.settings.google_solar_api_key,
                    },
                )
                response.raise_for_status()
                solar = self._parse_google_solar_response(response.json())
                if solar.maxPanels <= 0 or solar.estimatedKwPeak <= 0:
                    return self._mock_solar_data(
                        geocode,
                        "Google Solar API returned no usable panel potential",
                    )
                return solar
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return self._mock_solar_data(geocode, "Google Solar API request failed")

    async def fetch_roof_visualization(
        self,
        geocode: GeocodeResult,
        lead_id: str,
    ) -> RoofVisualization:
        image_id = _safe_image_id(lead_id)

        if not self.settings.maps_static_api_key:
            return RoofVisualization(
                roofImageSource=RoofImageSource.UNAVAILABLE,
                imageWarning="Google Maps Static API key missing",
            )

        try:
            timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.settings.google_maps_static_url,
                    params={
                        "center": f"{geocode.latitude},{geocode.longitude}",
                        "zoom": self.settings.google_maps_static_zoom,
                        "size": self.settings.google_maps_static_size,
                        "scale": self.settings.google_maps_static_scale,
                        "maptype": "satellite",
                        "format": "png",
                        "key": self.settings.maps_static_api_key,
                    },
                )
                if not response.is_success:
                    return RoofVisualization(
                        roofImageSource=RoofImageSource.UNAVAILABLE,
                        imageWarning=_maps_static_error_message(response),
                    )

                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    return RoofVisualization(
                        roofImageSource=RoofImageSource.UNAVAILABLE,
                        imageWarning=_maps_static_error_message(response),
                    )

                IMAGE_CACHE[image_id] = CachedImage(
                    content=response.content,
                    media_type=content_type.split(";")[0] or "image/png",
                )
                return RoofVisualization(
                    roofImageUrl=f"/agent2/roof-image/{image_id}.png",
                    roofImageSource=RoofImageSource.GOOGLE_MAPS_STATIC,
                )
        except httpx.HTTPError:
            return RoofVisualization(
                roofImageSource=RoofImageSource.UNAVAILABLE,
                imageWarning="Google Maps Static image request failed",
            )

    def get_cached_image(self, image_id: str) -> CachedImage | None:
        return IMAGE_CACHE.get(image_id) or IMAGE_CACHE.get(_safe_image_id(image_id))

    def get_cached_image_for_url(self, url: str | None) -> CachedImage | None:
        if not url:
            return None
        return self.get_cached_image(_image_id_from_url(url))

    def _parse_google_solar_response(self, payload: dict[str, Any]) -> SolarPotential:
        potential = payload.get("solarPotential") or {}

        max_panels = int(potential.get("maxArrayPanelsCount") or 0)
        panel_capacity_watts = float(potential.get("panelCapacityWatts") or 400)
        panel_width_meters = float(potential.get("panelWidthMeters") or 1.134)
        panel_height_meters = float(potential.get("panelHeightMeters") or 1.722)
        estimated_kw_peak = round((max_panels * panel_capacity_watts) / 1000, 1)
        sunshine_hours = float(potential.get("maxSunshineHoursPerYear") or 950)

        selected_config = self._best_panel_config(
            potential.get("solarPanelConfigs") or []
        )
        if selected_config is not None:
            yearly_energy_kwh = int(
                round(float(selected_config.get("yearlyEnergyDcKwh") or 0))
            )
        else:
            yearly_energy_kwh = int(round(estimated_kw_peak * sunshine_hours))

        roof_summaries = []
        if selected_config:
            roof_summaries = selected_config.get("roofSegmentSummaries") or []
        if not roof_summaries:
            roof_summaries = potential.get("roofSegmentStats") or []

        orientation_score = self._roof_orientation_score(roof_summaries)
        pitch_score = self._roof_pitch_score(roof_summaries)
        return SolarPotential(
            maxPanels=max_panels,
            panelCapacityWatts=panel_capacity_watts,
            panelWidthMeters=panel_width_meters,
            panelHeightMeters=panel_height_meters,
            maxSunshineHoursPerYear=round(sunshine_hours, 1),
            estimatedKwPeak=max(estimated_kw_peak, 0),
            yearlyEnergyKwh=max(yearly_energy_kwh, 0),
            roofOrientationScore=round(orientation_score, 2),
            roofPitchScore=round(pitch_score, 2),
            roofSuitability=self._suitability_from_metrics(
                estimated_kw_peak=estimated_kw_peak,
                yearly_energy_kwh=yearly_energy_kwh,
                orientation_score=orientation_score,
                pitch_score=pitch_score,
            ),
            dataSource=SolarDataSource.GOOGLE_SOLAR_API,
            imageryQuality=payload.get("imageryQuality"),
        )

    def _mock_solar_data(
        self,
        geocode: GeocodeResult,
        reason: str | None = None,
    ) -> SolarPotential:
        address = geocode.formattedAddress.lower()
        commercial_markers = (
            "autohaus",
            "logistik",
            "lagerhalle",
            "produktion",
            "grosshandel",
            "großhandel",
            "baumarkt",
            "supermarkt",
            "fitnessstudio",
            "moebelhaus",
            "möbelhaus",
            "gewerbepark",
        )
        if "frankfurt" in address and any(
            marker in address for marker in commercial_markers
        ):
            return SolarPotential(
                maxPanels=28,
                panelCapacityWatts=410,
                panelWidthMeters=1.134,
                panelHeightMeters=1.722,
                maxSunshineHoursPerYear=1090,
                estimatedKwPeak=11.5,
                yearlyEnergyKwh=12100,
                roofOrientationScore=0.86,
                roofPitchScore=0.78,
                roofSuitability=RoofSuitability.GOOD,
                dataSource=SolarDataSource.MOCK,
                imageryQuality="DEMO_ESTIMATE",
                fallbackReason=reason,
            )

        if "frankfurt" in address or "60311" in address:
            return SolarPotential(
                maxPanels=19,
                panelCapacityWatts=400,
                panelWidthMeters=1.134,
                panelHeightMeters=1.722,
                maxSunshineHoursPerYear=1105,
                estimatedKwPeak=7.6,
                yearlyEnergyKwh=8400,
                roofOrientationScore=0.89,
                roofPitchScore=0.82,
                roofSuitability=RoofSuitability.GOOD,
                dataSource=SolarDataSource.MOCK,
                imageryQuality="DEMO_ESTIMATE",
                fallbackReason=reason,
            )

        seed_source = f"{geocode.latitude:.5f}:{geocode.longitude:.5f}:{address}"
        digest = hashlib.sha256(seed_source.encode("utf-8")).hexdigest()
        seed = int(digest[:12], 16)

        max_panels = 12 + (seed % 20)
        panel_capacity = 400 + ((seed // 19) % 5) * 10
        estimated_kw_peak = round((max_panels * panel_capacity) / 1000, 1)

        orientation_score = round(0.58 + ((seed // 37) % 38) / 100, 2)
        pitch_score = round(0.62 + ((seed // 71) % 32) / 100, 2)
        sunshine_hours = 900 + ((seed // 97) % 240)
        production_factor = 0.88 + (orientation_score * 0.07) + (pitch_score * 0.05)
        yearly_energy_kwh = int(
            round(estimated_kw_peak * sunshine_hours * production_factor / 50) * 50
        )

        return SolarPotential(
            maxPanels=max_panels,
            panelCapacityWatts=panel_capacity,
            panelWidthMeters=1.134,
            panelHeightMeters=1.722,
            maxSunshineHoursPerYear=sunshine_hours,
            estimatedKwPeak=estimated_kw_peak,
            yearlyEnergyKwh=yearly_energy_kwh,
            roofOrientationScore=orientation_score,
            roofPitchScore=pitch_score,
            roofSuitability=self._suitability_from_metrics(
                estimated_kw_peak=estimated_kw_peak,
                yearly_energy_kwh=yearly_energy_kwh,
                orientation_score=orientation_score,
                pitch_score=pitch_score,
            ),
            dataSource=SolarDataSource.MOCK,
            imageryQuality="DEMO_ESTIMATE",
            fallbackReason=reason,
        )

    def _best_panel_config(
        self,
        panel_configs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not panel_configs:
            return None
        return max(
            panel_configs,
            key=lambda config: float(config.get("yearlyEnergyDcKwh") or 0),
        )

    def _roof_orientation_score(self, roof_summaries: list[dict[str, Any]]) -> float:
        if not roof_summaries:
            return 0.72

        weighted_score = 0.0
        total_weight = 0.0
        for summary in roof_summaries:
            pitch = float(summary.get("pitchDegrees") or 0)
            azimuth = float(summary.get("azimuthDegrees") or 180)
            weight = float(
                summary.get("panelsCount")
                or summary.get("yearlyEnergyDcKwh")
                or 1
            )
            score = 0.82 if pitch <= 5 else _azimuth_score_for_germany(azimuth)
            weighted_score += score * weight
            total_weight += weight

        return weighted_score / total_weight if total_weight else 0.72

    def _roof_pitch_score(self, roof_summaries: list[dict[str, Any]]) -> float:
        if not roof_summaries:
            return 0.72

        weighted_score = 0.0
        total_weight = 0.0
        for summary in roof_summaries:
            pitch = float(summary.get("pitchDegrees") or 0)
            weight = float(
                summary.get("panelsCount")
                or summary.get("yearlyEnergyDcKwh")
                or 1
            )
            if pitch <= 5:
                score = 0.72
            else:
                score = _clamp(1 - (abs(pitch - 35) / 45), 0.25, 1.0)
            weighted_score += score * weight
            total_weight += weight

        return weighted_score / total_weight if total_weight else 0.72

    def _suitability_from_metrics(
        self,
        estimated_kw_peak: float,
        yearly_energy_kwh: int,
        orientation_score: float,
        pitch_score: float,
    ) -> RoofSuitability:
        if estimated_kw_peak <= 0:
            return RoofSuitability.POOR

        yield_per_kwp = yearly_energy_kwh / estimated_kw_peak
        combined_score = (
            _clamp((estimated_kw_peak - 3.5) / 8) * 0.25
            + _clamp((yield_per_kwp - 700) / 450) * 0.35
            + orientation_score * 0.25
            + pitch_score * 0.15
        )

        if combined_score >= 0.82:
            return RoofSuitability.EXCELLENT
        if combined_score >= 0.65:
            return RoofSuitability.GOOD
        if combined_score >= 0.45:
            return RoofSuitability.MODERATE
        return RoofSuitability.POOR


def _azimuth_score_for_germany(azimuth_degrees: float) -> float:
    diff_from_south = abs(((azimuth_degrees - 180 + 180) % 360) - 180)
    return _clamp(1 - (diff_from_south / 180) * 0.85, 0.15, 1.0)


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _safe_image_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    safe = "".join(char for char in value if char.isalnum() or char in {"-", "_"})
    return f"{safe[:48] or 'roof'}-{digest}"


def _maps_static_error_message(response: httpx.Response) -> str:
    text = response.text.strip()
    if text:
        return f"Google Maps Static image unavailable: {text[:220]}"
    return f"Google Maps Static image unavailable: HTTP {response.status_code}"


def _image_id_from_url(url: str) -> str:
    filename = url.rsplit("/", 1)[-1]
    return filename.removesuffix(".png")
