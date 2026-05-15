from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()


@dataclass
class Settings:
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or None
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    google_solar_api_key: str | None = os.getenv("GOOGLE_SOLAR_API_KEY") or None
    speechmatics_api_key: str | None = os.getenv("SPEECHMATICS_API_KEY") or None
    vapi_api_key: str | None = os.getenv("VAPI_API_KEY") or None
    vapi_assistant_id: str | None = os.getenv("VAPI_ASSISTANT_ID") or None
    vapi_phone_number_id: str | None = os.getenv("VAPI_PHONE_NUMBER_ID") or None
    vapi_call_url: str = os.getenv("VAPI_CALL_URL", "https://api.vapi.ai/call")
    vapi_file_url: str = os.getenv("VAPI_FILE_URL", "https://api.vapi.ai/file")
    google_calendar_id: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    google_application_credentials: str | None = (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None
    )
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    agent2_base_url: str = os.getenv("AGENT2_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    booking_base_url: str | None = (os.getenv("BOOKING_BASE_URL") or None)
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/solar_agent.db")
    app_timezone: str = os.getenv("APP_TIMEZONE", "Europe/Berlin")
    smtp_host: str | None = os.getenv("SMTP_HOST") or None
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str | None = os.getenv("SMTP_USER") or None
    smtp_password: str | None = os.getenv("SMTP_PASSWORD") or None
    from_email: str = os.getenv("FROM_EMAIL", "solar-lead-os@example.com")
    staff_notify_email: str = os.getenv("STAFF_NOTIFY_EMAIL", "sales-team@example.com")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def sqlite_path(self) -> str:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// DATABASE_URL values are supported in v1.")
        return self.database_url.removeprefix("sqlite:///")

    @property
    def vapi_configured(self) -> bool:
        return bool(
            self.vapi_api_key
            and self.vapi_assistant_id
            and self.vapi_phone_number_id
        )


settings = Settings()
