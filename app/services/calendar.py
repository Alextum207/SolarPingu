from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
    reused: bool = False


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


def _busy_events(
    start: datetime,
    end: datetime,
    calendar_id: str | None = None,
) -> list[tuple[datetime, datetime]]:
    service = _google_service()
    if service is None:
        return []
    response = (
        service.events()
        .list(
            calendarId=calendar_id or settings.google_calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    bounds = [_event_bounds(event) for event in response.get("items", [])]
    return [item for item in bounds if item is not None]


def _existing_booking(
    service: Any,
    *,
    calendar_id: str,
    start: datetime,
    end: datetime,
    summary: str,
    description: str,
    idempotency_key: str | None,
) -> CalendarBooking | None:
    response = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue
        private = (event.get("extendedProperties") or {}).get("private") or {}
        if idempotency_key and private.get("solar_lead_booking_key") == idempotency_key:
            return CalendarBooking(
                event_id=event["id"],
                html_link=event.get("htmlLink"),
                reused=True,
            )
        bounds = _event_bounds(event)
        if not bounds:
            continue
        event_start, event_end = bounds
        if (
            event.get("summary") == summary
            and event_start == start
            and event_end == end
            and event.get("description") == description
        ):
            return CalendarBooking(
                event_id=event["id"],
                html_link=event.get("htmlLink"),
                reused=True,
            )
    return None


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < busy_end and end > busy_start for busy_start, busy_end in busy)


def _slot_label(start: datetime) -> str:
    return start.strftime("%a, %d.%m. %H:%M Uhr")


def get_available_slots(max_slots: int = 24, calendar_id: str | None = None) -> list[Slot]:
    now = datetime.now(settings.tz)
    window_start = now
    window_end = now + timedelta(days=LOOKAHEAD_DAYS)
    busy = _busy_events(window_start, window_end, calendar_id=calendar_id)
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


def is_slot_available(start: datetime, calendar_id: str | None = None) -> bool:
    candidate = start.astimezone(settings.tz)
    try:
        slots = get_available_slots(max_slots=300, calendar_id=calendar_id)
    except TypeError:
        slots = get_available_slots(max_slots=300)
    return any(
        slot.start == candidate
        for slot in slots
    )


def book_qualification_call(
    *,
    name: str,
    email: str,
    phone: str,
    address: str,
    message: str,
    start: datetime,
    end: datetime,
    calendar_id: str | None = None,
    idempotency_key: str | None = None,
    replace_event_id: str | None = None,
    replace_calendar_id: str | None = None,
) -> CalendarBooking:
    service = _google_service()
    if service is None:
        return CalendarBooking(event_id=replace_event_id or f"local-{uuid4().hex}", html_link=None)

    target_calendar_id = calendar_id or settings.google_calendar_id
    summary = f"Solar Vor-Ort-Planung - {name}"
    description = (
        f"Name: {name}\nEmail: {email}\nTelefon: {phone}\n"
        f"Adresse: {address}\nNotiz: {message}"
    )
    existing = _existing_booking(
        service,
        calendar_id=target_calendar_id,
        start=start,
        end=end,
        summary=summary,
        description=description,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return existing

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.app_timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.app_timezone},
    }
    if idempotency_key:
        event["extendedProperties"] = {
            "private": {
                "solar_lead_booking_key": idempotency_key,
            }
        }
    if replace_event_id:
        source_calendar_id = replace_calendar_id or target_calendar_id
        if source_calendar_id == target_calendar_id:
            try:
                updated = (
                    service.events()
                    .patch(
                        calendarId=target_calendar_id,
                        eventId=replace_event_id,
                        body=event,
                    )
                    .execute()
                )
                return CalendarBooking(event_id=updated["id"], html_link=updated.get("htmlLink"))
            except HttpError as exc:
                if exc.resp.status not in {404, 410}:
                    raise
        else:
            try:
                service.events().delete(
                    calendarId=source_calendar_id,
                    eventId=replace_event_id,
                ).execute()
            except HttpError as exc:
                if exc.resp.status not in {404, 410}:
                    raise
    created = (
        service.events()
        .insert(
            calendarId=target_calendar_id,
            body=event,
        )
        .execute()
    )
    return CalendarBooking(event_id=created["id"], html_link=created.get("htmlLink"))
