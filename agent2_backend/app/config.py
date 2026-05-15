from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Solar Lead OS - Feasibility Ops Agent"
    app_version: str = "0.3.0"
    environment: str = "local"

    use_mock_geocoding: bool = True
    use_mock_solar: bool = True

    google_geocoding_api_key: str | None = None
    google_solar_api_key: str | None = None
    google_maps_static_api_key: str | None = None
    google_places_api_key: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    featherless_api_key: str | None = None
    featherless_base_url: str = "https://api.featherless.ai/v1"
    featherless_vision_model: str = "google/gemma-3-27b-it"
    agent1_webhook_url: str | None = None

    electricity_price_per_kwh: float = Field(default=0.34, gt=0)
    feed_in_tariff_per_kwh: float = Field(default=0.08, ge=0)
    self_consumption_ratio: float = Field(default=0.55, ge=0, le=1)
    base_price_per_kwp_min: float = Field(default=1580, gt=0)
    base_price_per_kwp_max: float = Field(default=2040, gt=0)
    battery_addon_price_min: int = Field(default=2500, ge=0)
    battery_addon_price_max: int = Field(default=3000, ge=0)

    external_api_timeout_seconds: float = Field(default=8.0, gt=0)

    google_geocoding_url: str = "https://maps.googleapis.com/maps/api/geocode/json"
    google_solar_url: str = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
    google_maps_static_url: str = "https://maps.googleapis.com/maps/api/staticmap"
    google_places_text_search_url: str = "https://places.googleapis.com/v1/places:searchText"
    gemini_api_base_url: str = "https://generativelanguage.googleapis.com"

    google_maps_static_zoom: int = Field(default=20, ge=0, le=21)
    google_maps_static_size: str = "640x360"
    google_maps_static_scale: int = Field(default=2, ge=1, le=4)
    use_mock_places: bool = True
    finder_max_places_per_run: int = Field(default=30, ge=1, le=100)
    agent1_webhook_max_attempts: int = Field(default=2, ge=1, le=5)

    model_config = SettingsConfigDict(
        env_file=("backend/.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def geocoding_api_key(self) -> str | None:
        return self.google_geocoding_api_key or self.google_solar_api_key

    @property
    def maps_static_api_key(self) -> str | None:
        return (
            self.google_maps_static_api_key
            or self.google_geocoding_api_key
            or self.google_solar_api_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
