from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.models import Slot
from app.services import calendar, email, speechmatics, twilio_bridge, vapi
from app.services.twilio_bridge import create_customer_call as real_twilio_create_customer_call
from app.services import gemini


@pytest.fixture(autouse=True)
def _mock_customer_call(monkeypatch):
    async def fake_customer_call(**kwargs):
        return {"sid": "customer_call_123", "status": "queued", "mock": True}

    monkeypatch.setattr(twilio_bridge, "create_customer_call", fake_customer_call)
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "smtp_user", None)
    monkeypatch.setattr(settings, "smtp_password", None)
    monkeypatch.setattr(settings, "google_application_credentials", None)


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


def test_finder_lead_webhook_stores_agentic_lead(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'finder.db'}")
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    db.init_db()

    with TestClient(app) as client:
        response = client.post(
            "/api/finder/leads",
            json={
                "leadId": "FINDER-TEST-1",
                "source": "GOOGLE_PLACES",
                "businessName": "Autohaus Test",
                "category": "Autohaus",
                "address": "Industriestrasse 1, Frankfurt, Germany",
                "phone": "+4969000000",
                "website": "https://example.com",
                "googleMapsUrl": "https://maps.google.com/?q=test",
                "rating": 4.5,
                "solar": {"estimatedKwPeak": 12.4, "decision": "PURSUE"},
                "vision": {
                    "visualSolarPotentialScore": 0.78,
                    "roofType": "flat_commercial_roof",
                    "blockers": [],
                },
                "publicInfoOnly": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["lead_id"] == "FINDER-TEST-1"
        assert response.json()["handoffUrl"].endswith("/api/leads/FINDER-TEST-1/handoff")

        stored = client.get("/api/leads/FINDER-TEST-1")
        assert stored.status_code == 200
        payload = stored.json()
        assert payload["status"] == "finder_lead_received"
        assert payload["intake"]["name"] == "Autohaus Test"
        assert payload["solar"]["finderSolar"]["estimatedKwPeak"] == 12.4
        assert payload["solar"]["finderVision"]["visualSolarPotentialScore"] == 0.78


def test_finder_leads_list_supports_control_board(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'finder_list.db'}")
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    db.init_db()

    with TestClient(app) as client:
        response = client.post(
            "/api/finder/leads",
            json={
                "leadId": "FINDER-LIST-1",
                "businessName": "Logistik Test",
                "category": "Logistik",
                "address": "Logistikring 2, Frankfurt, Germany",
                "phone": "+4969000001",
                "solar": {"estimatedKwPeak": 14.1, "decision": "PURSUE"},
                "vision": {"visualSolarPotentialScore": 0.81, "roofType": "flat", "blockers": []},
            },
        )
        assert response.status_code == 200

        listing = client.get("/api/finder/leads")
        assert listing.status_code == 200
        payload = listing.json()
        assert payload["count"] == 1
        assert payload["leads"][0]["lead_id"] == "FINDER-LIST-1"
        assert payload["leads"][0]["intake"]["name"] == "Logistik Test"
        assert payload["leads"][0]["solar"]["finderSolar"]["estimatedKwPeak"] == 14.1


def test_unified_leads_list_supports_operator_dashboard(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'operator.db'}")
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    db.init_db()

    with TestClient(app) as client:
        project_response = client.post(
            "/api/projects",
            json={"city": "Frankfurt am Main", "name": "Frankfurt Projekt A"},
        )
        assert project_response.status_code == 200
        project = project_response.json()["project"]

        website = client.post("/api/intake", json=_pursue_payload())
        assert website.status_code == 200

        finder = client.post(
            "/api/finder/leads",
            json={
                "leadId": "FINDER-OP-1",
                "projectId": project["project_id"],
                "businessName": "B2B Lead GmbH",
                "category": "Logistik",
                "address": "Industriestrasse 4, Frankfurt, Germany",
                "phone": "+4969000002",
                "solar": {
                    "estimatedKwPeak": 16.2,
                    "profitabilityScore": 0.88,
                    "decision": "PURSUE",
                },
                "vision": {"visualSolarPotentialScore": 0.82, "roofType": "flat", "blockers": []},
            },
        )
        assert finder.status_code == 200

        all_leads = client.get("/api/leads")
        assert all_leads.status_code == 200
        payload = all_leads.json()
        assert payload["count"] == 2
        assert {lead["source"] for lead in payload["leads"]} == {"website", "b2b_finder"}
        assert all(lead["updated_at"] for lead in payload["leads"])

        website_only = client.get("/api/leads?source=website")
        assert website_only.status_code == 200
        assert website_only.json()["count"] == 1
        assert website_only.json()["leads"][0]["source"] == "website"

        finder_only = client.get("/api/leads?source=b2b_finder")
        assert finder_only.status_code == 200
        assert finder_only.json()["count"] == 1
        assert finder_only.json()["leads"][0]["lead_id"] == "FINDER-OP-1"
        assert finder_only.json()["leads"][0]["project_id"] == project["project_id"]
        assert finder_only.json()["leads"][0]["project_city"] == "Frankfurt am Main"

        project_leads = client.get(f"/api/leads?project_id={project['project_id']}")
        assert project_leads.status_code == 200
        assert project_leads.json()["count"] == 1
        assert project_leads.json()["leads"][0]["lead_id"] == "FINDER-OP-1"

        projects = client.get("/api/projects")
        assert projects.status_code == 200
        assert projects.json()["projects"][0]["lead_count"] == 1
        assert projects.json()["projects"][0]["b2b_count"] == 1


def test_workflow_stream_emits_operator_log_events(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'stream.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    db.init_db()

    with TestClient(app) as client:
        intake = client.post("/api/intake", json=_pursue_payload())
        lead_id = intake.json()["lead_id"]

        with client.stream("GET", f"/api/workflows/{lead_id}/stream") as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "event: trace" in body
        assert "Workflow starten" in body
        assert "Profitability Agent" in body
        assert "event: final" in body
        assert "event: done" in body

        listing = client.get(f"/api/leads?source=website&status=booking_link_sent")
        assert listing.status_code == 200
        lead = listing.json()["leads"][0]
        assert lead["offerPdfUrl"].endswith(f"/api/leads/{lead_id}/offer.pdf")
        assert lead["decision"] == "PURSUE"


def test_finder_stream_proxy_normalizes_agent2_events(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent2_base_url", "http://agent2.local")

    class FakeStreamResponse:
        def raise_for_status(self) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def aiter_lines(self):
            lines = [
                "event: trace",
                'data: {"event":{"step":"Finder gestartet","tool":"BusinessFinderService","status":"RUNNING","thought":"Suche startet","detail":"city=Frankfurt"}}',
                "",
                "event: final",
                'data: {"response":{"runId":"RUN-1","city":"Frankfurt","qualifiedCount":1,"sentToAgent1Count":1,"leads":[]}}',
                "",
                "event: done",
                "data: {}",
                "",
            ]
            for line in lines:
                yield line

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        def stream(self, method: str, url: str, params: dict):
            assert method == "GET"
            assert url == "http://agent2.local/finder/stream"
            assert params == {"city": "Frankfurt"}
            return FakeStreamResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    with TestClient(app) as client:
        with client.stream("GET", "/api/finder/stream?city=Frankfurt") as response:
            body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert "Finder verbinden" in body
    assert "Finder gestartet" in body
    assert '"agent": "Agent 2"' in body
    assert "event: final" in body
    assert "event: done" in body


def test_finder_stream_assigns_final_leads_to_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'project_stream.db'}")
    monkeypatch.setattr(settings, "agent2_base_url", "http://agent2.local")
    db.init_db()

    class FakeStreamResponse:
        def raise_for_status(self) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def aiter_lines(self):
            lines = [
                "event: final",
                'data: {"response":{"runId":"RUN-PROJECT","city":"Frankfurt","qualifiedCount":1,"sentToAgent1Count":1,"leads":[{"leadId":"FINDER-PROJECT-1"}]}}',
                "",
                "event: done",
                "data: {}",
                "",
            ]
            for line in lines:
                yield line

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        def stream(self, method: str, url: str, params: dict):
            assert method == "GET"
            assert url == "http://agent2.local/finder/stream"
            assert params == {"city": "Frankfurt"}
            return FakeStreamResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    with TestClient(app) as client:
        project = client.post(
            "/api/projects",
            json={"city": "Frankfurt", "name": "Frankfurt Projekt"},
        ).json()["project"]
        client.post(
            "/api/finder/leads",
            json={
                "leadId": "FINDER-PROJECT-1",
                "businessName": "Projekt Lead GmbH",
                "address": "Industriestrasse 1, Frankfurt",
                "phone": "+4969000003",
            },
        )

        with client.stream(
            "GET",
            f"/api/finder/stream?city=Frankfurt&project_id={project['project_id']}",
        ) as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "assignedLeadIds" in body
        project_leads = client.get(f"/api/leads?project_id={project['project_id']}")
        assert project_leads.json()["count"] == 1
        assert project_leads.json()["leads"][0]["lead_id"] == "FINDER-PROJECT-1"


def test_finder_run_proxies_to_agent2_for_frontend(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent2_base_url", "http://agent2.local")
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "runId": "RUN-PROXY",
                "city": "Frankfurt am Main",
                "discoveredCount": 1,
                "qualifiedCount": 1,
                "sentToAgent1Count": 1,
                "leads": [],
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, json: dict):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    with TestClient(app) as client:
        response = client.post("/api/finder/run", json={"city": "Frankfurt am Main"})

    assert response.status_code == 200
    assert response.json()["runId"] == "RUN-PROXY"
    assert calls == [
        ("http://agent2.local/finder/run", {"city": "Frankfurt am Main"})
    ]


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
        assert voice.json()["summary_email"]["status"] == "demo_logged"


def test_agentic_recording_upload_accepts_speechmatics_hybrid(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'hybrid.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "speechmatics_api_key", None)
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        upload = client.post(
            "/api/recordings",
            data={"lead_id": lead["lead_id"]},
            files={"audio_file": ("browser-voice-demo.webm", b"fake audio", "audio/webm")},
        )

    assert upload.status_code == 200
    assert upload.json()["lead_id"] == lead["lead_id"]
    assert upload.json()["speechmatics_job_id"].startswith("mock-")


def test_workflow_page_starts_customer_call_without_pdf_or_voice_demo(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'workflow_page.db'}")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "speechmatics_api_key", None)
    db.init_db()

    with TestClient(app) as client:
        payload = _pursue_payload()
        payload["email_address"] = payload.pop("email")
        response = client.post("/intake", data=payload)

    assert response.status_code == 200
    assert "Wir rufen Sie gleich" in response.text
    assert "customer_call_123" in response.text
    assert "Angebots-PDF" not in response.text
    assert "Voice Demo starten" not in response.text
    assert "Vapi Demo-Call starten" not in response.text


def test_twilio_twiml_connects_conversation_relay(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'twiml.db'}")
    monkeypatch.setattr(settings, "public_base_url", "https://agent1.example.com")
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        response = client.post(f"/webhooks/twilio/voice/{lead['lead_id']}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<ConversationRelay" in response.text
    assert 'url="wss://agent1.example.com/ws/twilio/conversation/' in response.text
    assert 'code="multi"' in response.text
    assert "Vapi" not in response.text


def test_twilio_customer_call_posts_async_form_payload(monkeypatch) -> None:
    monkeypatch.setattr(settings, "twilio_account_sid", "AC_TEST")
    monkeypatch.setattr(settings, "twilio_auth_token", "token_test")
    monkeypatch.setattr(settings, "twilio_from_number", "+12762431540")
    monkeypatch.setattr(settings, "twilio_call_url", "https://api.twilio.test/2010-04-01")
    monkeypatch.setattr(settings, "public_base_url", "https://agent1.example.com")
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 201
        text = ""

        def json(self) -> dict[str, str]:
            return {"sid": "CA_TEST", "status": "queued"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, *, content=None, headers=None, auth=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            captured["auth"] = auth
            return FakeResponse()

    monkeypatch.setattr(twilio_bridge.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        real_twilio_create_customer_call(
            lead_id="SL-TEST",
            customer_number="+4917662355154",
        )
    )

    assert result == {"sid": "CA_TEST", "status": "queued"}
    assert captured["url"] == "https://api.twilio.test/2010-04-01/Accounts/AC_TEST/Calls.json"
    assert captured["auth"] == ("AC_TEST", "token_test")
    assert captured["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
    form = parse_qs(captured["content"])
    assert form["Url"] == ["https://agent1.example.com/webhooks/twilio/voice/SL-TEST"]
    assert form["RecordingStatusCallback"] == [
        "https://agent1.example.com/webhooks/twilio/recording/SL-TEST"
    ]


def test_twilio_conversation_relay_websocket_uses_gemini(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'twilio_ws.db'}")
    db.init_db()
    calls = []

    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return (
            "Bei Ihrer groben Planung sprechen wir ueber rund 11 kWp und eine "
            "Amortisation um 10 Jahre. Beim E-Auto lohnt es sich besonders, wenn "
            "Sie viel mit eigenem Solarstrom laden."
        )

    monkeypatch.setattr("app.services.twilio_bridge.gemini.generate_text", fake_generate_text)

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        db.update_agentic_artifacts(
            lead_id,
            status="call_scheduled",
            solar={
                "solar_potential": {
                    "estimated_kwp": 11.6,
                    "yearly_energy_kwh": 8541,
                }
            },
            profitability={
                "estimated_kwp": 11.6,
                "estimated_price_min": 23320,
                "estimated_price_max": 27517,
                "payback_years": 9.6,
            },
            offer={
                "system_size_kwp": 11.6,
                "includes_battery": True,
                "price_range": {"min": 23320, "max": 27517, "currency": "EUR"},
            },
        )
        with client.websocket_connect(f"/ws/twilio/conversation/{lead_id}") as websocket:
            websocket.send_json(
                {
                    "type": "setup",
                    "sessionId": "VX123",
                    "callSid": "CA123",
                    "customParameters": {"lead_id": lead_id},
                }
            )
            websocket.send_json(
                {
                    "type": "prompt",
                    "voicePrompt": "Ich habe ein E-Auto und Sorge, ob sich Speicher und Solar wirklich lohnen.",
                    "lang": "de-DE",
                    "last": True,
                }
            )
            response = websocket.receive_json()

    assert response["type"] == "text"
    assert response["token"].startswith("Bei Ihrer groben Planung")
    assert "lang" not in response
    assert calls[0]["payload"]["detected_language"] == "de-DE"
    assert calls[0]["payload"]["business_case"]["system_size_kwp"]
    assert calls[0]["payload"]["business_case"]["payback_years"]
    assert "Do not jump straight to an installer appointment" in calls[0]["system_prompt"]
    assert "never ask for the same concern again" in calls[0]["system_prompt"]


def test_gemini_text_returns_fallback_on_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    class RateLimitedResponse:
        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://example.test")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    class RateLimitedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return RateLimitedResponse()

    monkeypatch.setattr("app.services.gemini.httpx.AsyncClient", RateLimitedClient)

    import asyncio

    response = asyncio.run(
        gemini.generate_text(
            system_prompt="Reply briefly.",
            payload={"prompt": "Hallo"},
            fallback="Fallback bleibt im Call.",
        )
    )

    assert response == "Fallback bleibt im Call."


def test_gemini_structured_json_returns_fallback_on_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    fallback = {"ok": True, "source": "fallback"}

    class RateLimitedResponse:
        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://example.test")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    class RateLimitedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return RateLimitedResponse()

    monkeypatch.setattr("app.services.gemini.httpx.AsyncClient", RateLimitedClient)

    import asyncio

    response = asyncio.run(
        gemini.generate_structured_json(
            system_prompt="Return JSON.",
            payload={"prompt": "Hallo"},
            fallback=fallback,
        )
    )

    assert response == fallback


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
    assert "Anna Becker" in markdown
    assert "do not interrupt" in markdown


def test_conversation_summary_includes_agent2_plan(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'email_plan.db'}")
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()

    result = email.send_conversation_summary(
        lead_id="L-PLAN",
        lead_name="Anna Becker",
        lead_email="anna@example.com",
        lead_phone="+491700000000",
        source="Test",
        call_summary="Kunde will Sorgen klaeren und dann Vor-Ort-Termin.",
        qualification={
            "need": "Stromkosten senken",
            "main_concern": "Speicher lohnt sich eventuell nicht",
            "call_outcome": "ready_to_book",
        },
        planning_context={
            "profitability": {"decision": "PURSUE", "score": 82, "resource_level": "high_touch"},
            "offer": {
                "package_name": "Smart PV Paket",
                "price_range": {"min": 18000, "max": 24000, "currency": "EUR"},
                "next_steps": ["Vor-Ort-Termin"],
            },
            "available_slots": [{"label": "Mo, 18.05. 10:00 Uhr"}],
            "installers": [
                {
                    "id": "solar_emergies",
                    "name": "Solar Emergies",
                    "region": "Berlin",
                    "available_slots": [
                        {
                            "value": "2026-05-18T10:00:00+02:00",
                            "label": "Mo, 18.05. 10:00 Uhr",
                        },
                        {
                            "value": "2026-05-18T14:00:00+02:00",
                            "label": "Mo, 18.05. 14:00 Uhr",
                        },
                    ],
                },
                {
                    "id": "partner_west",
                    "name": "Partner West",
                    "region": "NRW",
                    "available_slots": [
                        {
                            "value": "2026-05-19T09:30:00+02:00",
                            "label": "Di, 19.05. 09:30 Uhr",
                        }
                    ],
                },
            ],
            "handoff": {"demo_url": "https://example.test/demo/L-PLAN"},
        },
    )

    assert result["status"] == "demo_logged"
    assert "Lead information" in result["body"]
    assert "Agent-2-Dashboard" in result["body"]
    assert "/dashboard/leads/L-PLAN" in result["body"]
    assert "Bild von den moeglichen Solar Panels von Agent 2" not in result["body"]
    assert "/api/leads/L-PLAN/panel-plan.png" not in result["body"]
    assert "/installer/confirm/L-PLAN" in result["body"]
    assert "installer_id=solar_emergies" in result["body"]
    assert "installer_id=partner_west" in result["body"]
    assert "Mo, 18.05. 10:00 Uhr" in result["body"]
    assert "Di, 19.05. 09:30 Uhr" in result["body"]
    assert "finales Vor-Ort-Planungsgespraech" in result["body"]
    assert result["html_body"] is not None
    assert "Agent-2-Details im Dashboard oeffnen" in result["html_body"]
    assert "Freie Termine aus dem Telefonat auswaehlen" in result["html_body"]
    assert "Partner West" in result["html_body"]
    assert "Call-Audio" in result["html_body"]
    assert "Gespraechszusammenfassung" in result["html_body"]


def test_dashboard_lead_detail_alias_opens_exact_lead(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'dashboard.db'}")
    monkeypatch.setattr(settings, "dashboard_url_template", "http://127.0.0.1:5175/?leadId={lead_id}")
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        response = client.get(f"/dashboard/leads/{lead['lead_id']}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"http://127.0.0.1:5175/?leadId={lead['lead_id']}"


def test_panel_plan_image_and_installer_confirm(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'installer.db'}")
    monkeypatch.setattr(settings, "google_application_credentials", None)
    monkeypatch.setattr(
        settings,
        "installers_json",
        '[{"id":"installer_a","name":"Installer A","calendar_id":"calendar-a@example.com","region":"Berlin"}]',
    )
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        client.post(f"/api/workflows/{lead_id}/run")
        image = client.get(f"/api/leads/{lead_id}/panel-plan.png")
        slot = (datetime.now(settings.tz) + timedelta(days=2)).replace(
            hour=11,
            minute=0,
            second=0,
            microsecond=0,
        )
        confirm = client.get(
            f"/installer/confirm/{lead_id}",
            params={"installer_id": "installer_a", "slot": slot.isoformat()},
        )
        confirm_again = client.get(
            f"/installer/confirm/{lead_id}",
            params={"installer_id": "installer_a", "slot": slot.isoformat()},
        )
        moved_slot = slot + timedelta(hours=1)
        move_confirm = client.get(
            f"/installer/confirm/{lead_id}",
            params={"installer_id": "installer_a", "slot": moved_slot.isoformat()},
        )
        stored = db.get_agentic_lead(lead_id)

    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert confirm.status_code == 200
    assert confirm_again.status_code == 200
    assert move_confirm.status_code == 200
    assert "Handwerker-Termin bestaetigt" in confirm.text
    assert "kein zweiter Kalendertermin erstellt" in confirm_again.text
    assert "wurde auf diesen Slot verschoben" in move_confirm.text
    assert stored is not None
    assert stored["status"] == "installer_appointment_confirmed"
    assert (stored.get("voice") or {}).get("installer_appointment", {}).get("confirmed") is True
    assert (
        (stored.get("voice") or {}).get("installer_appointment", {}).get("start")
        == moved_slot.isoformat()
    )
    assert (
        (stored.get("voice") or {}).get("installer_appointment", {}).get("installer_id")
        == "installer_a"
    )
    assert (
        (stored.get("voice") or {}).get("installer_appointment", {}).get("calendar_id")
        == "calendar-a@example.com"
    )


def test_customer_booking_request_waits_for_installer_confirmation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'customer_booking.db'}")
    monkeypatch.setattr(settings, "google_application_credentials", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "public_base_url", "https://agent1.example.com")
    monkeypatch.setattr(
        settings,
        "installers_json",
        '[{"id":"installer_a","name":"Installer A","calendar_id":"calendar-a@example.com","region":"Berlin"}]',
    )
    db.init_db()

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        client.post(f"/api/workflows/{lead_id}/run")
        book = client.get(f"/book/{lead_id}", params={"mode": "in_person"})
        slot = (datetime.now(settings.tz) + timedelta(days=3)).replace(
            hour=13,
            minute=0,
            second=0,
            microsecond=0,
        )
        request_booking = client.post(
            f"/book/{lead_id}",
            data={
                "mode": "in_person",
                "installer_id": "installer_a",
                "slot": slot.isoformat(),
            },
        )
        requested = db.get_agentic_lead(lead_id)
        confirm = client.get(
            f"/installer/confirm/{lead_id}",
            params={"installer_id": "installer_a", "slot": slot.isoformat(), "mode": "in_person"},
        )
        confirmed = db.get_agentic_lead(lead_id)

    assert book.status_code == 200
    assert "Vor-Ort-Termin" in book.text
    assert request_booking.status_code == 200
    assert "Terminwunsch ist eingegangen" in request_booking.text
    assert requested is not None
    assert requested["status"] == "customer_slot_requested"
    request_payload = (requested.get("voice") or {}).get("customer_booking_request") or {}
    assert request_payload["status"] == "pending_installer_confirmation"
    assert request_payload["installer_email_status"] == "demo_logged"
    assert confirm.status_code == 200
    assert confirmed is not None
    voice = confirmed.get("voice") or {}
    assert voice.get("installer_appointment", {}).get("confirmed") is True
    assert voice.get("customer_appointment_confirmation", {}).get("status") == "demo_logged"


def test_twilio_recording_callback_sends_downloadable_audio(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'recording.db'}")
    monkeypatch.setattr(settings, "public_base_url", "https://agent1.example.com")
    db.init_db()

    async def fake_download(recording_url: str, *, lead_id: str):
        return {
            "filename": f"solar-call-{lead_id}.mp3",
            "content": b"fake-audio",
            "content_type": "audio/mpeg",
            "media_url": f"{recording_url}.mp3",
        }

    monkeypatch.setattr(twilio_bridge, "download_recording_audio", fake_download)

    with TestClient(app) as client:
        lead = client.post("/api/intake", json=_pursue_payload()).json()
        lead_id = lead["lead_id"]
        callback = client.post(
            f"/webhooks/twilio/recording/{lead_id}",
            data={
                "CallSid": "CA123",
                "RecordingSid": "RE123",
                "RecordingUrl": "https://api.twilio.com/Recordings/RE123",
                "RecordingStatus": "completed",
                "RecordingDuration": "42",
            },
        )
        audio = client.get(f"/api/leads/{lead_id}/call-audio?recording_sid=RE123")
        stored = db.get_agentic_lead(lead_id)

    assert callback.status_code == 200
    assert callback.json()["summary_email"]["status"] == "demo_logged"
    assert callback.json()["summary_email"]["attachments"] == [f"solar-call-{lead_id}.mp3"]
    assert audio.status_code == 200
    assert audio.content == b"fake-audio"
    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert stored is not None
    recording = (stored.get("voice") or {}).get("twilio_recording") or {}
    assert recording["recording_sid"] == "RE123"
    assert recording["download_url"].endswith(f"/api/leads/{lead_id}/call-audio?recording_sid=RE123")
    assert (stored.get("voice") or {}).get("twilio_recording_email", {}).get("recording_sid") == "RE123"


def test_speechmatics_callback_artifacts_group_speaker_turns() -> None:
    artifacts = speechmatics.callback_artifacts(
        {
            "job": {"tracking": {"lead_id": "L-SPEECH"}},
            "results": [
                {
                    "type": "word",
                    "start_time": 0.1,
                    "end_time": 0.4,
                    "alternatives": [
                        {"content": "Hallo", "speaker": "S1", "confidence": 0.98}
                    ],
                },
                {
                    "type": "word",
                    "start_time": 0.5,
                    "end_time": 0.8,
                    "alternatives": [
                        {"content": "Anna", "speaker": "S1", "confidence": 0.95}
                    ],
                },
                {
                    "type": "punctuation",
                    "start_time": 0.8,
                    "end_time": 0.8,
                    "alternatives": [
                        {"content": ".", "speaker": "S1", "confidence": 1.0}
                    ],
                },
                {
                    "type": "word",
                    "start_time": 1.2,
                    "end_time": 1.6,
                    "alternatives": [
                        {"content": "Ja", "speaker": "S2", "confidence": 0.7}
                    ],
                },
            ],
        }
    )

    assert artifacts["lead_id"] == "L-SPEECH"
    assert artifacts["transcript"] == "Hallo Anna. Ja"
    assert artifacts["conversation_turns"][0]["speaker"] == "S1"
    assert artifacts["conversation_turns"][0]["text"] == "Hallo Anna."
    assert artifacts["conversation_turns"][1]["speaker"] == "S2"
    assert artifacts["low_confidence_terms"][0]["content"] == "Ja"


def test_vapi_extract_call_text_supports_end_report_payload() -> None:
    summary, transcript = vapi.extract_call_text(
        {
            "message": {
                "type": "end-of-call-report",
                "analysis": {"summary": "Kunde moechte einen Termin."},
                "artifact": {
                    "messages": [
                        {"role": "assistant", "message": "Hallo Anna"},
                        {"role": "user", "message": "Ich moechte buchen"},
                    ]
                },
            }
        }
    )

    assert summary == "Kunde moechte einen Termin."
    assert "assistant: Hallo Anna" in transcript
    assert "user: Ich moechte buchen" in transcript
