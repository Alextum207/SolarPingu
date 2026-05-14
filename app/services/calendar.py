from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import settings
from app.models import Slot


SLOT_MINUTES = 30
LOOKAHEAD_DAYS = 14
MIN_LEAD_TIME_HOURS = 2
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17


@dataclass(frozen=True)
class CalendarBooking:
    event_id: str
    html_link: str | None = None


def _google_service() -> Any | None:
    if not settings.google_application_credentials:
        return None
    credentials = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _event_bounds(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start_raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end_raw = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
    if not start_raw or not end_raw:
        return None
    return (datetime.fromisoformat(start_raw.replace("Z", "+00:00")), datetime.fromisoformat(end_raw.replace("Z", "+00:00")))


def _busy_events(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    service = _google_service()
    if service is None:
        return []
    response = (
        service.events()
        .list(
            calendarId=settings.google_calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    bounds = [_event_bounds(event) for event in response.get("items", [])]
    return [item for item in bounds if item is not None]


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < busy_end and end > busy_start for busy_start, busy_end in busy)


def _slot_label(start: datetime) -> str:
    return start.strftime("%a, %d.%m. %H:%M Uhr")


def get_available_slots(max_slots: int = 24) -> list[Slot]:
    now = datetime.now(settings.tz)
    window_start = now
    window_end = now + timedelta(days=LOOKAHEAD_DAYS)
    busy = _busy_events(window_start, window_end)
    slots: list[Slot] = []

    for offset in range(LOOKAHEAD_DAYS):
        day = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        if day.weekday() >= 5:
            continue
        for hour in range(WORKDAY_START_HOUR, WORKDAY_END_HOUR):
            for minute in (0, 30):
                start = day.replace(hour=hour, minute=minute)
                end = start + timedelta(minutes=SLOT_MINUTES)
                if start < now + timedelta(hours=MIN_LEAD_TIME_HOURS):
                    continue
                if _overlaps(start, end, busy):
                    continue
                slots.append(
                    Slot(
                        value=start.isoformat(),
                        label=_slot_label(start),
                        start=start,
                        end=end,
                    )
                )
                if len(slots) >= max_slots:
                    return slots
    return slots


def is_slot_available(start: datetime) -> bool:
    candidate = start.astimezone(settings.tz)
    return any(slot.start == candidate for slot in get_available_slots(max_slots=300))


def book_qualification_call(
    *,
    name: str,
    email: str,
    phone: str,
    address: str,
    message: str,
    start: datetime,
    end: datetime,
) -> CalendarBooking:
    service = _google_service()
    if service is None:
        return CalendarBooking(event_id=f"local-{uuid4().hex}", html_link=None)

    event = {
        "summary": f"Solar Qualifikation - {name}",
        "description": (
            f"Name: {name}\nEmail: {email}\nTelefon: {phone}\n"
            f"Adresse: {address}\nNotiz: {message}"
        ),
        "start": {"dateTime": start.isoformat(), "timeZone": settings.app_timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.app_timezone},
        "attendees": [{"email": email}],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    created = (
        service.events()
        .insert(
            calendarId=settings.google_calendar_id,
            body=event,
            conferenceDataVersion=1,
            sendUpdates="all",
        )
        .execute()
    )
    return CalendarBooking(event_id=created["id"], html_link=created.get("htmlLink"))
