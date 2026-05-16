from __future__ import annotations

from typing import Any

from app.config import settings
from app.services import calendar


def configured_installers() -> list[dict[str, Any]]:
    return [
        {
            "id": str(installer.get("id")),
            "name": str(installer.get("name") or installer.get("id")),
            "calendar_id": str(installer.get("calendar_id")),
            "region": str(installer.get("region") or "Standardgebiet"),
        }
        for installer in settings.installers
    ]


def get_installer(installer_id: str | None) -> dict[str, Any]:
    installers = configured_installers()
    if installer_id:
        for installer in installers:
            if installer["id"] == installer_id:
                return installer
    return installers[0]


def installer_slot_options(max_slots_per_installer: int = 3) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for installer in configured_installers():
        try:
            slots = [
                slot.model_dump(mode="json")
                for slot in calendar.get_available_slots(
                    max_slots=max_slots_per_installer,
                    calendar_id=installer["calendar_id"],
                )
            ]
            error = None
        except Exception as exc:
            slots = []
            error = str(exc)
        options.append(
            {
                **installer,
                "available_slots": slots,
                "error": error,
            }
        )
    return options
