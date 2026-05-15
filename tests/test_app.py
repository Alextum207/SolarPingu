from __future__ import annotations

import base64
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.models import Slot
from app.services import calendar, gemini, offer_pdf, vapi


@pytest.fixture(autouse=True)
def disable_featherless_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "featherless_api_key", None)


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


@pytest.mark.asyncio
async def test_featherless_generates_when_gemini_key_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "featherless_api_key", "featherless-test")
    monkeypatch.setattr(settings, "featherless_base_url", "https://api.featherless.ai/v1")
    monkeypatch.setattr(settings, "featherless_model", "test/model")
    posts = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": '{"provider":"featherless","ok":true}'}}
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs):
            posts.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr(gemini.httpx, "AsyncClient", FakeAsyncClient)

    result = await gemini.generate_structured_json(
        system_prompt="Return JSON",
        payload={"lead": "test"},
        fallback={"fallback": True},
    )

    assert result == {"provider": "featherless", "ok": True}
    assert posts[0][0] == "https://api.featherless.ai/v1/chat/completions"
    assert posts[0][1]["json"]["model"] == "test/model"
    assert posts[0][1]["headers"]["Authorization"] == "Bearer featherless-test"


@pytest.mark.asyncio
async def test_featherless_fallback_runs_after_gemini_http_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(settings, "gemini_model", "gemini-test-model")
    monkeypatch.setattr(settings, "featherless_api_key", "featherless-test")
    monkeypatch.setattr(settings, "featherless_base_url", "https://api.featherless.ai/v1")
    monkeypatch.setattr(settings, "featherless_model", "fallback/model")
    urls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"fallback":"featherless"}'}}]}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs):
            urls.append(url)
            if "generativelanguage.googleapis.com" in url:
                raise httpx.ConnectError("Gemini unavailable")
            return FakeResponse()

    monkeypatch.setattr(gemini.httpx, "AsyncClient", FakeAsyncClient)

    result = await gemini.generate_structured_json(
        system_prompt="Return JSON",
        payload={"lead": "test"},
        fallback={"fallback": "local"},
    )

    assert result == {"fallback": "featherless"}
    assert len(urls) == 2
    assert "gemini-test-model:generateContent" in urls[0]
    assert urls[1] == "https://api.featherless.ai/v1/chat/completions"


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
                "roofImageUrl": "/agent2/roof-image/FINDER-TEST-1.png",
                "roofImageSource": "GOOGLE_MAPS_STATIC",
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
        assert payload["solar"]["roofImageUrl"].endswith("/agent2/roof-image/FINDER-TEST-1.png")
        assert payload["solar"]["roofImageSource"] == "GOOGLE_MAPS_STATIC"


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
                "roofImageUrl": "/agent2/roof-image/FINDER-OP-1.png",
                "roofImageSource": "GOOGLE_MAPS_STATIC",
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
        assert finder_only.json()["leads"][0]["roofImageUrl"].endswith(
            "/agent2/roof-image/FINDER-OP-1.png"
        )

        project_leads = client.get(f"/api/leads?project_id={project['project_id']}")
        assert project_leads.status_code == 200
        assert project_leads.json()["count"] == 1
        assert project_leads.json()["leads"][0]["lead_id"] == "FINDER-OP-1"

        projects = client.get("/api/projects")
        assert projects.status_code == 200
        assert projects.json()["projects"][0]["lead_count"] == 1
        assert projects.json()["projects"][0]["b2b_count"] == 1


def test_agent2_roof_image_is_used_in_offer_pdf(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'roof_image_pdf.db'}")
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    monkeypatch.setattr(settings, "agent2_base_url", "http://agent2.local")
    monkeypatch.setattr(settings, "google_solar_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_host", None)
    db.init_db()
    fetched_urls = []
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    def fake_fetch_image_bytes(url: str) -> bytes:
        fetched_urls.append(url)
        return png_1x1

    monkeypatch.setattr(offer_pdf, "_fetch_image_bytes", fake_fetch_image_bytes)

    with TestClient(app) as client:
        response = client.post(
            "/api/finder/leads",
            json={
                "leadId": "FINDER-PDF-IMAGE",
                "businessName": "Bild Dach GmbH",
                "category": "Logistik",
                "address": "Dachstrasse 2, Frankfurt, Germany",
                "phone": "+4969000003",
                "roofImageUrl": "/agent2/roof-image/FINDER-PDF-IMAGE.png",
                "roofImageSource": "GOOGLE_MAPS_STATIC",
                "solar": {
                    "estimatedKwPeak": 18.2,
                    "profitabilityScore": 0.91,
                    "decision": "PURSUE",
                },
                "vision": {"visualSolarPotentialScore": 0.84, "roofType": "flat", "blockers": []},
            },
        )
        assert response.status_code == 200

        workflow = client.post("/api/workflows/FINDER-PDF-IMAGE/run")
        assert workflow.status_code == 200
        image_url = "http://agent2.local/agent2/roof-image/FINDER-PDF-IMAGE.png"
        assert image_url in fetched_urls

        stored = client.get("/api/leads/FINDER-PDF-IMAGE")
        assert stored.status_code == 200
        assert stored.json()["solar"]["roofImageUrl"] == image_url

        pdf = client.get("/api/leads/FINDER-PDF-IMAGE/offer.pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
        assert fetched_urls.count(image_url) >= 2


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
