from fastapi.testclient import TestClient

from app import main
from app.models import (
    Agent1DeliveryStatus,
    BusinessLeadSource,
    Decision,
    FinderLead,
    FinderRunResponse,
    FinderSolarSummary,
    FinderTraceEvent,
    VisionAnalysis,
)


def test_finder_run_endpoint(monkeypatch) -> None:
    async def fake_run(request):
        return FinderRunResponse(
            runId="RUN-TEST",
            city=request.city,
            discoveredCount=1,
            qualifiedCount=1,
            sentToAgent1Count=0,
            trace=[
                FinderTraceEvent(
                    step="Finder gestartet",
                    tool="BusinessFinderService",
                    status="DONE",
                    thought="Test trace event",
                    detail="Frankfurt am Main",
                )
            ],
            leads=[
                FinderLead(
                    leadId="FINDER-123",
                    source=BusinessLeadSource.MOCK,
                    businessName="Autohaus Frankfurt am Main",
                    category="Autohaus",
                    address="Industriestrasse 1, Frankfurt am Main, Germany",
                    roofImageUrl="/agent2/roof-image/test.png",
                    solar=FinderSolarSummary(
                        estimatedKwPeak=10.4,
                        yearlyEnergyKwh=9800,
                        panelCount=26,
                        profitabilityScore=0.7,
                        decision=Decision.PURSUE,
                    ),
                    vision=VisionAnalysis(
                        visualSolarPotentialScore=0.76,
                        roofType="flat_commercial_roof",
                        blockers=[],
                        confidence=0.81,
                    ),
                    qualified=True,
                    qualificationReason="Qualified by Agent 2 and Featherless vision",
                    sentToAgent1=False,
                    agent1Status=Agent1DeliveryStatus.SKIPPED,
                    agent1Warning="AGENT1_WEBHOOK_URL missing; lead not sent",
                )
            ],
        )

    monkeypatch.setattr(main.business_finder_service, "run", fake_run)
    client = TestClient(main.app)

    response = client.post("/finder/run", json={"city": "Frankfurt am Main"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["runId"] == "RUN-TEST"
    assert payload["qualifiedCount"] == 1
    assert payload["trace"][0]["tool"] == "BusinessFinderService"
    assert payload["leads"][0]["businessName"] == "Autohaus Frankfurt am Main"
