from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.models import Slot
from app.services import calendar, vapi


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_index_loads() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Agentic Workflow starten" in response.text


def test_create_lead_and_callback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(settings, "gemini_api_key", None)
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

    async def fake_vapi_call(**kwargs):
        return {"id": "call_test_123", "mock": True}

    monkeypatch.setattr(vapi, "create_outbound_call", fake_vapi_call)

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
        assert data["lead"]["status"] == "call_scheduled"
        assert data["vapi_call"]["id"] == "call_test_123"
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


def _pursue_payload() -> dict:
    return {
        "name": "Anna Becker",
        "email": "anna@example.com",
        "phone": "+4915112345678",
        "address": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
        "owner_status": "owner",
        "roof_type": "pitched",
        "need": "both",
        "timeline": "within_3_months",
        "budget_range": "20000-30000",
        "decision_maker": "Ich entscheide mit meinem Partner",
        "main_concern": "Finanzierung",
        "battery_interest": True,
        "wallbox_interest": False,
        "preferred_contact": "email",
    }


def test_agentic_workflow_pursue(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'agentic.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()

    with TestClient(app) as client:
        intake = client.post("/api/intake", json=_pursue_payload())
        assert intake.status_code == 200
        lead_id = intake.json()["lead_id"]

        workflow = client.post(f"/api/workflows/{lead_id}/run")
        assert workflow.status_code == 200
        data = workflow.json()
        assert data["profitability"]["decision"] == "PURSUE"
        assert data["email"]["status"] == "demo_logged"
        assert data["offer"]["lead_id"] == lead_id

        handoff = client.get(f"/api/leads/{lead_id}/handoff")
        assert handoff.status_code == 200
        assert handoff.json()["source"] == "solar-agent-fastapi"
        assert handoff.json()["offer_pdf_url"].endswith(f"/api/leads/{lead_id}/offer.pdf")

        pdf = client.get(f"/api/leads/{lead_id}/offer.pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")


def test_agentic_workflow_reject(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'reject.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()

    with TestClient(app) as client:
        intake = client.post(
            "/api/intake",
            json={
                "name": "Max Mieter",
                "email": "max@example.com",
                "phone": "+4915111111111",
                "address": "Demo Strasse 1, Berlin",
                "owner_status": "renter",
                "roof_type": "unknown",
                "need": "cost_savings",
                "timeline": "exploring",
                "budget_range": "under_10000",
                "decision_maker": "Vermieter",
                "main_concern": "Nur guenstig",
                "battery_interest": False,
                "wallbox_interest": False,
                "preferred_contact": "email",
            },
        )
        lead_id = intake.json()["lead_id"]
        workflow = client.post(f"/api/workflows/{lead_id}/run")
        assert workflow.status_code == 200
        data = workflow.json()
        assert data["profitability"]["decision"] == "REJECT"
        assert data["profitability"]["next_action"] == "polite_reject"


def test_agent2_evaluate_persists_pdf_for_hub(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    db.init_db()

    with TestClient(app) as client:
        response = client.post(
            "/agent2/evaluate",
            json={
                "leadId": "L-HUB-PDF",
                "name": "Anna Becker",
                "phone": "+4915112345678",
                "address": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
                "ownerStatus": "owner",
                "budgetRange": "20000-30000",
                "installationTimeline": "within_3_months",
                "batteryInterest": True,
                "objections": ["Finanzierung"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["offerPdfUrl"].endswith("/api/leads/L-HUB-PDF/offer.pdf")

        pdf = client.get("/api/leads/L-HUB-PDF/offer.pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")

        handoff = client.get("/api/leads/L-HUB-PDF/handoff")
        assert handoff.status_code == 200
        assert handoff.json()["offer"]["lead_id"] == "L-HUB-PDF"


def test_voice_session_closes_and_notifies(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'voice.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        client.post(f"/api/workflows/{lead_id}/run")
        voice = client.post(
            "/api/voice/session",
            json={"lead_id": lead_id, "prompt": "Passt, machen wir. Ich will abschliessen."},
        )
        assert voice.status_code == 200
        assert voice.json()["intent"] == "closed"
        assert voice.json()["staff_notification_required"] is True


def test_vapi_offer_call_button(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'vapi_offer.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()

    async def fake_offer_call(**kwargs):
        assert kwargs["offer_pdf_url"].endswith(".pdf")
        return {"id": "offer_call_123", "status": "queued"}

    monkeypatch.setattr(vapi, "create_offer_demo_call", fake_offer_call)

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        client.post(f"/api/workflows/{lead_id}/run")
        call = client.post(
            f"/api/leads/{lead_id}/vapi-offer-call",
            data={"phone_number": "+15551234567"},
        )
        assert call.status_code == 200
        assert "offer_call_123" in call.text


def test_vapi_offer_context_markdown_contains_call_file_payload() -> None:
    markdown = vapi.build_offer_context_markdown(
        lead_id="L-CONTEXT",
        customer_name="Anna Becker",
        customer_number="+4915112345678",
        customer_email="anna@example.com",
        offer={
            "package_name": "Smart PV Paket",
            "system_size_kwp": 9.8,
            "includes_battery": True,
            "price_range": {"min": 22000, "max": 28000, "currency": "EUR"},
            "value_pitch": ["Mehr Eigenverbrauch"],
            "assumptions": ["Vor-Ort-Pruefung offen"],
            "next_steps": ["Termin buchen"],
        },
        profitability={
            "decision": "PURSUE",
            "score": 82,
            "resource_level": "high_touch",
            "estimated_margin": 5200,
            "payback_years": 9.2,
            "reasons": ["Eigentuemerstatus bestaetigt"],
            "disqualifiers": [],
        },
        offer_pdf_url="http://testserver/api/leads/L-CONTEXT/offer.pdf",
    )

    assert "Anna Becker" in markdown
    assert "Smart PV Paket" in markdown
    assert "http://testserver/api/leads/L-CONTEXT/offer.pdf" in markdown
    assert "Voice Agent Instructions" in markdown
