from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.models import Slot
from app.services import calendar


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_index_loads() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Solar-Erstgespräch buchen" in response.text


def test_create_lead_and_callback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db()

    start = (datetime.now(settings.tz) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    slot = Slot(
        value=start.isoformat(),
        label="Test slot",
        start=start,
        end=start + timedelta(minutes=30),
    )
    monkeypatch.setattr(calendar, "get_available_slots", lambda max_slots=24: [slot])

    with TestClient(app) as client:
        response = client.post(
            "/api/leads",
            json={
                "name": "Alex Test",
                "email": "alex@example.com",
                "phone": "+491701234567",
                "address": "Solarstr. 1, Berlin",
                "message": "Interessiert an Speicher",
                "preferred_slot": start.isoformat(),
            },
        )
        assert response.status_code == 200
        data = response.json()
        lead_id = data["lead"]["lead_id"]
        assert data["lead"]["status"] == "scheduled"
        assert data["call_plan"]["next_action"] == "schedule_call"

        recording = client.post(
            "/api/recordings",
            data={"lead_id": lead_id, "recording_url": "https://example.com/call.wav"},
        )
        assert recording.status_code == 200
        assert recording.json()["lead_id"] == lead_id

        callback = client.post(
            "/webhooks/speechmatics",
            json={
                "job": {"tracking": {"lead_id": lead_id}},
                "transcript": "Der Kunde besitzt das Haus und hat ein geneigtes Dach.",
            },
        )
        assert callback.status_code == 200
        assert callback.json()["ok"] is True

        stored = client.get(f"/api/leads/{lead_id}")
        assert stored.status_code == 200
        assert stored.json()["status"] == "transcribed"
