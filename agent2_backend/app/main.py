import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.config import get_settings
from app.models import (
    AddressEvaluationRequest,
    BusinessSearchRequest,
    FinderRunResponse,
    HealthResponse,
    ProjectDecision,
)
from app.models import model_to_jsonable
from app.services.agent1 import Agent1WebhookService
from app.services.decision import DecisionService
from app.services.evaluation import EvaluationService
from app.services.finder import BusinessFinderService
from app.services.geocoding import GeocodingService
from app.services.places import PlacesService
from app.services.solar import SolarService
from app.services.vision import FeatherlessVisionService

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Agent 2: address-first Feasibility Ops Agent for Solar Lead OS.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

geocoding_service = GeocodingService(settings)
solar_service = SolarService(settings)
decision_service = DecisionService(settings)
evaluation_service = EvaluationService(
    settings=settings,
    geocoding_service=geocoding_service,
    solar_service=solar_service,
    decision_service=decision_service,
)
places_service = PlacesService(settings)
vision_service = FeatherlessVisionService(settings)
agent1_service = Agent1WebhookService(settings)
business_finder_service = BusinessFinderService(
    settings=settings,
    places_service=places_service,
    evaluation_service=evaluation_service,
    solar_service=solar_service,
    vision_service=vision_service,
    agent1_service=agent1_service,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> HTMLResponse:
    return HTMLResponse(_demo_html())


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        mockGeocodingEnabled=settings.use_mock_geocoding,
        mockSolarEnabled=settings.use_mock_solar,
        mockPlacesEnabled=settings.use_mock_places,
        geminiConfigured=bool(settings.gemini_api_key),
        featherlessConfigured=bool(settings.featherless_api_key),
        agent1WebhookConfigured=bool(settings.agent1_webhook_url),
    )


@app.post("/agent2/evaluate", response_model=ProjectDecision)
async def evaluate(request: AddressEvaluationRequest) -> ProjectDecision:
    return await evaluation_service.evaluate(request)


@app.post("/finder/run", response_model=FinderRunResponse)
async def run_finder(request: BusinessSearchRequest) -> FinderRunResponse:
    return await business_finder_service.run(request)


@app.get("/finder/stream", include_in_schema=False)
async def stream_finder(city: str) -> StreamingResponse:
    request = BusinessSearchRequest(city=city)
    return StreamingResponse(
        _finder_event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/agent2/sample-response", response_model=ProjectDecision)
async def sample_response() -> ProjectDecision:
    sample = AddressEvaluationRequest(
        leadId="L-001",
        address="Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
        name="Anna Becker",
        batteryInterest=True,
        budgetRange="15000-20000",
        installationTimeline="within_3_months",
        ownerStatus="owner",
        objections=["financing uncertainty"],
    )
    return await evaluate(sample)


@app.get("/agent2/roof-image/{image_id}.png", include_in_schema=False)
async def roof_image(image_id: str) -> Response:
    cached_image = solar_service.get_cached_image(image_id)
    if cached_image is None:
        raise HTTPException(status_code=404, detail="Roof image not found or expired")
    return Response(content=cached_image.content, media_type=cached_image.media_type)


async def _finder_event_stream(request: BusinessSearchRequest):
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def emit_trace(event) -> None:
        await queue.put({"type": "trace", "event": model_to_jsonable(event)})

    async def run_job() -> None:
        try:
            response = await business_finder_service.run(
                request,
                trace_callback=emit_trace,
            )
            await queue.put({"type": "final", "response": model_to_jsonable(response)})
        except Exception as exc:
            await queue.put(
                {
                    "type": "fail",
                    "message": f"{exc.__class__.__name__}: {str(exc)[:300]}",
                }
            )
        finally:
            await queue.put({"type": "done"})

    task = asyncio.create_task(run_job())
    try:
        while True:
            item = await queue.get()
            event_type = str(item.pop("type"))
            yield f"event: {event_type}\n"
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if event_type in {"done", "fail"}:
                break
    finally:
        if not task.done():
            task.cancel()


def _demo_html() -> str:
    return """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Solar Lead OS</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f4;
      --panel: #ffffff;
      --ink: #17211c;
      --muted: #5c6860;
      --line: #d9dfd8;
      --accent: #167f5f;
      --accent-dark: #0f6249;
      --warn: #ab6a05;
      --reject: #a23b3b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    main {
      width: min(1160px, calc(100% - 32px));
      margin: 0 auto;
      padding: 30px 0;
    }
    header {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 22px;
    }
    h1 {
      margin: 0;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1.05;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .status {
      border: 1px solid var(--line);
      background: #edf5f1;
      color: var(--accent-dark);
      padding: 8px 10px;
      border-radius: 8px;
      font-size: 14px;
      white-space: nowrap;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 420px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .sidebar {
      display: grid;
      gap: 18px;
    }
    .tool, .result {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 16px 40px rgba(23, 33, 28, 0.06);
    }
    label {
      display: block;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
      margin: 14px 0 6px;
    }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    input:focus, select:focus {
      outline: 3px solid rgba(22, 127, 95, 0.18);
      border-color: var(--accent);
    }
    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .check {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 14px;
      color: var(--ink);
      font-weight: 650;
    }
    .check input { width: 18px; height: 18px; }
    button {
      width: 100%;
      margin-top: 18px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      padding: 13px 14px;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
    }
    button:disabled { opacity: .65; cursor: wait; }
    .result { min-height: 420px; }
    .roof-image {
      width: 100%;
      aspect-ratio: 16 / 9;
      border: 1px solid var(--line);
      border-radius: 8px;
      object-fit: cover;
      background: #e7ece7;
      display: block;
      margin-bottom: 14px;
    }
    .image-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin: -6px 0 14px;
    }
    .image-missing {
      aspect-ratio: 16 / 9;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #f7faf8;
      color: var(--muted);
      display: grid;
      place-items: center;
      text-align: center;
      padding: 18px;
      margin-bottom: 14px;
      line-height: 1.45;
    }
    .empty {
      color: var(--muted);
      min-height: 360px;
      display: grid;
      place-items: center;
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 24px;
    }
    .decision {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
      margin-bottom: 14px;
    }
    .decision strong {
      display: block;
      margin-top: 8px;
      font-size: 24px;
      line-height: 1.15;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 750;
      background: #edf5f1;
      color: var(--accent-dark);
      white-space: nowrap;
    }
    .pill.NURTURE { background: #fff2d6; color: var(--warn); }
    .pill.REJECT { background: #f8e1e1; color: var(--reject); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin: 16px 0;
    }
    .metric, .fact {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 82px;
      background: #fff;
    }
    .fact { background: #f7faf8; }
    .metric span, .fact span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .metric b, .fact b {
      display: block;
      margin-top: 7px;
      font-size: 22px;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .fact b { font-size: 16px; }
    .facts {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 14px;
    }
    .reasoning {
      color: var(--muted);
      line-height: 1.45;
      margin: 14px 0 0;
    }
    .error {
      color: var(--reject);
      border: 1px solid #efc6c6;
      background: #fff5f5;
      border-radius: 8px;
      padding: 12px;
      margin-top: 12px;
      overflow-wrap: anywhere;
    }
    .warning {
      color: #79520b;
      border: 1px solid #efd28d;
      background: #fff8e6;
      border-radius: 8px;
      padding: 11px 12px;
      margin-bottom: 14px;
      line-height: 1.4;
    }
    .finder-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 14px;
    }
    .trace {
      margin: 12px 0 18px;
      padding: 4px 0;
    }
    .trace-list {
      display: grid;
      gap: 12px;
    }
    .trace-item {
      color: var(--muted);
      animation: traceIn 220ms ease-out both;
      opacity: .72;
    }
    .trace-item:nth-last-child(1) { opacity: .95; }
    .trace-item:nth-last-child(2) { opacity: .78; }
    .trace-item:nth-last-child(3) { opacity: .62; }
    .trace-item.RUNNING {
      animation: traceIn 220ms ease-out both, traceFlicker 1.6s ease-in-out infinite;
    }
    .trace-tool-row {
      display: flex;
      align-items: center;
      gap: 9px;
      color: #777b78;
      font-size: 14px;
      line-height: 1.3;
      font-weight: 500;
      margin-bottom: 8px;
    }
    .trace-icon {
      width: 20px;
      height: 20px;
      display: inline-grid;
      place-items: center;
      color: #717670;
      flex: 0 0 auto;
    }
    .trace-chevron {
      color: #8b908b;
      font-size: 18px;
      line-height: 1;
      margin-left: 2px;
    }
    .trace-action {
      display: grid;
      grid-template-columns: 20px minmax(0, 1fr);
      gap: 9px;
      align-items: start;
      color: #2f332f;
      font-size: 15px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .trace-dot {
      width: 8px;
      height: 8px;
      margin: 7px 0 0 6px;
      border-radius: 50%;
      background: #242724;
    }
    .trace-subtext {
      color: #8a8f89;
      font-size: 11px;
      line-height: 1.35;
      margin-top: 3px;
      overflow-wrap: anywhere;
    }
    .trace-item.RUNNING .trace-dot {
      animation: pulseDot 1.2s ease-in-out infinite;
    }
    .trace-item.WARN .trace-dot { background: var(--warn); }
    .trace-item.FAILED .trace-dot { background: var(--reject); }
    .trace-item.SKIPPED .trace-dot { background: #8a8f89; }
    @keyframes traceIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulseDot {
      0%, 100% { opacity: .45; transform: scale(.92); }
      50% { opacity: 1; transform: scale(1.08); }
    }
    @keyframes traceFlicker {
      0%, 100% { filter: brightness(1); }
      42% { filter: brightness(1.03); }
      45% { filter: brightness(.96); }
      48% { filter: brightness(1.04); }
      52% { filter: brightness(1); }
    }
    .lead-list {
      display: grid;
      gap: 0;
      border-top: 1px solid var(--line);
    }
    .lead-row {
      display: grid;
      grid-template-columns: 156px minmax(0, 1fr);
      gap: 14px;
      padding: 14px 0;
      border-bottom: 1px solid var(--line);
      align-items: start;
    }
    .lead-image {
      width: 100%;
      aspect-ratio: 4 / 3;
      border: 1px solid var(--line);
      border-radius: 8px;
      object-fit: cover;
      background: #e7ece7;
    }
    .lead-image-missing {
      width: 100%;
      aspect-ratio: 4 / 3;
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      display: grid;
      place-items: center;
      text-align: center;
      padding: 10px;
      font-size: 12px;
    }
    .lead-title {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 7px;
    }
    .lead-title strong {
      font-size: 18px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .lead-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .lead-stats {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .lead-reason {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      margin: 10px 0 0;
    }
    .pill.QUALIFIED, .pill.SENT { background: #edf5f1; color: var(--accent-dark); }
    .pill.UNQUALIFIED, .pill.SKIPPED { background: #f0f2ef; color: var(--muted); }
    .pill.FAILED { background: #f8e1e1; color: var(--reject); }
    @media (max-width: 860px) {
      main { width: min(100% - 24px, 720px); padding-top: 20px; }
      header { align-items: start; flex-direction: column; }
      .layout { grid-template-columns: 1fr; }
      .row, .metrics, .facts { grid-template-columns: 1fr; }
      .decision { align-items: start; flex-direction: column; }
      .lead-row { grid-template-columns: 1fr; }
      .trace-tool-row { font-size: 13px; }
      .trace-action { font-size: 14px; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Solar Lead OS</h1>
      <div class="status" id="status">Agent 2 bereit</div>
    </header>
    <section class="layout">
      <div class="sidebar">
      <section class="tool" id="finderTool">
        <h2>Finder-Agent</h2>
        <form id="finderForm">
          <label for="finderCity">Stadt</label>
          <input id="finderCity" name="city" autocomplete="address-level2" required
            value="Frankfurt am Main" />
          <button id="finderSubmit" type="submit">Businesses finden</button>
          <div id="finderError"></div>
        </form>
      </section>

      <form class="tool" id="form">
        <h2>Agent 2 Einzeladresse</h2>
        <label for="address">Adresse</label>
        <input id="address" name="address" autocomplete="street-address" required
          value="Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany" />

        <div class="row">
          <div>
            <label for="leadId">Lead ID</label>
            <input id="leadId" name="leadId" value="L-001" />
          </div>
          <div>
            <label for="name">Name</label>
            <input id="name" name="name" value="Anna Becker" />
          </div>
        </div>

        <div class="row">
          <div>
            <label for="budgetRange">Budget</label>
            <select id="budgetRange" name="budgetRange">
              <option value="unknown">Unbekannt</option>
              <option value="10000-15000">10.000-15.000 EUR</option>
              <option value="15000-20000" selected>15.000-20.000 EUR</option>
              <option value="20000-30000">20.000-30.000 EUR</option>
              <option value="30000+">30.000+ EUR</option>
            </select>
          </div>
          <div>
            <label for="installationTimeline">Zeitplan</label>
            <select id="installationTimeline" name="installationTimeline">
              <option value="unknown">Unbekannt</option>
              <option value="within_1_month">1 Monat</option>
              <option value="within_3_months" selected>3 Monate</option>
              <option value="within_6_months">6 Monate</option>
              <option value="exploring">Orientierung</option>
            </select>
          </div>
        </div>

        <div class="row">
          <div>
            <label for="ownerStatus">Eigentum</label>
            <select id="ownerStatus" name="ownerStatus">
              <option value="unknown">Unbekannt</option>
              <option value="owner" selected>Eigentuemer</option>
              <option value="co_owner">Miteigentuemer</option>
              <option value="family_owner">Familie entscheidet</option>
              <option value="property_manager">Verwaltung</option>
              <option value="renter">Mieter</option>
            </select>
          </div>
          <div>
            <label for="objections">Einwaende</label>
            <input id="objections" name="objections" value="financing uncertainty" />
          </div>
        </div>

        <label class="check">
          <input id="batteryInterest" name="batteryInterest" type="checkbox" checked />
          Batterie interessant
        </label>

        <button id="submit" type="submit">Adresse pruefen</button>
        <div id="error"></div>
      </form>
      </div>

      <section class="result" id="result">
        <div class="empty">Stadt scannen oder einzelne Adresse pruefen.</div>
      </section>
    </section>
  </main>

  <script>
    const finderForm = document.getElementById("finderForm");
    const finderSubmit = document.getElementById("finderSubmit");
    const finderError = document.getElementById("finderError");
    const form = document.getElementById("form");
    const submit = document.getElementById("submit");
    const result = document.getElementById("result");
    const error = document.getElementById("error");
    const statusEl = document.getElementById("status");

    finderForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      finderError.innerHTML = "";
      finderSubmit.disabled = true;
      finderSubmit.textContent = "Suche...";
      statusEl.textContent = "Finder-Agent scannt Stadt";

      const city = String(new FormData(finderForm).get("city") || "").trim();
      const traceEvents = [];
      result.innerHTML = renderFinderPending(city);

      try {
        await streamFinderRun(city, traceEvents);
        statusEl.textContent = "Finder-Lauf fertig";
      } catch (err) {
        finderError.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
        statusEl.textContent = "Bitte Stadt pruefen";
      } finally {
        finderSubmit.disabled = false;
        finderSubmit.textContent = "Businesses finden";
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.innerHTML = "";
      submit.disabled = true;
      submit.textContent = "Pruefe...";
      statusEl.textContent = "Geocoding und Solar API";

      const formData = new FormData(form);
      const data = {
        address: formData.get("address"),
        batteryInterest: document.getElementById("batteryInterest").checked,
      };
      for (const key of ["leadId", "name", "budgetRange", "installationTimeline", "ownerStatus"]) {
        const value = String(formData.get(key) || "").trim();
        if (value && value !== "unknown") data[key] = value;
      }
      const objections = String(formData.get("objections") || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      if (objections.length) data.objections = objections;

      try {
        const response = await fetch("/agent2/evaluate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail ? JSON.stringify(payload.detail) : "Ungueltige Eingabe");
        }
        renderResult(payload);
        statusEl.textContent = "Entscheidung fertig";
      } catch (err) {
        error.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
        statusEl.textContent = "Bitte Eingabe pruefen";
      } finally {
        submit.disabled = false;
        submit.textContent = "Adresse pruefen";
      }
    });

    function percent(value) {
      return `${Math.round(value * 100)}%`;
    }

    function eur(value) {
      return new Intl.NumberFormat("de-DE", {
        style: "currency",
        currency: "EUR",
        maximumFractionDigits: 0
      }).format(value);
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function optionalText(value) {
      if (value === null || value === undefined || String(value).trim() === "") {
        return "-";
      }
      return String(value);
    }

    function streamFinderRun(city, traceEvents) {
      return new Promise((resolve, reject) => {
        const source = new EventSource(`/finder/stream?city=${encodeURIComponent(city)}`);
        let settled = false;

        source.addEventListener("trace", (event) => {
          const payload = JSON.parse(event.data);
          traceEvents.push(payload.event);
          statusEl.textContent = `${payload.event.tool}: ${payload.event.step}`;
          result.innerHTML = renderFinderLive(city, traceEvents);
        });

        source.addEventListener("final", (event) => {
          const payload = JSON.parse(event.data);
          renderFinderResult(payload.response);
          settled = true;
          source.close();
          resolve();
        });

        source.addEventListener("fail", (event) => {
          if (settled) return;
          source.close();
          try {
            const payload = JSON.parse(event.data);
            reject(new Error(payload.message || "Finder stream failed"));
          } catch {
            reject(new Error("Finder stream failed"));
          }
        });

        source.onerror = () => {
          if (settled) return;
          source.close();
          reject(new Error("Finder stream connection failed"));
        };
      });
    }

    function renderFinderPending(city) {
      const pendingTrace = [
        {
          step: `Suche nach aktuellen Informationen zu Solar-Leads in ${city || "der Stadt"}`,
          tool: "Searching the web",
          status: "RUNNING",
          thought: "Ich bereite die Suche vor und frage gleich Google Places nach passenden Gewerbe-Treffern.",
          detail: city
        },
        {
          step: "Pruefe geeignete Branchen: Autohaeuser, Logistik, Lagerhallen und Gewerbeparks",
          tool: "Planning search",
          status: "QUEUED",
          thought: "Danach pruefe ich jede Adresse mit Agent 2, Solar API, Maps Static und Featherless Vision.",
          detail: "Autohaus, Logistik, Lagerhalle, Produktion, Grosshandel, Baumarkt, Supermarkt, Fitnessstudio, Moebelhaus, Gewerbepark"
        }
      ];
      return `
        <div class="decision">
          <div>
            <span class="pill">FINDER</span>
            <strong>${escapeHtml(city || "Stadt")}</strong>
          </div>
          <span class="pill">RUNNING</span>
        </div>
        ${renderTrace(pendingTrace)}
        <div class="empty">Finder-Agent arbeitet...</div>
      `;
    }

    function renderFinderLive(city, traceEvents) {
      return `
        <div class="decision">
          <div>
            <span class="pill">FINDER</span>
            <strong>${escapeHtml(city || "Stadt")}</strong>
          </div>
          <span class="pill">LIVE</span>
        </div>
        ${renderTrace(traceEvents)}
        <div class="empty">Finder-Agent arbeitet live weiter...</div>
      `;
    }

    function renderTrace(events) {
      if (!events || !events.length) return "";
      const visibleEvents = events.slice(-3);
      const items = visibleEvents.map((event) => {
        const location = [event.businessName, event.address].filter(Boolean).join(" - ");
        const action = traceActionText(event);
        const subtext = [location, event.detail].filter(Boolean).join(" · ");
        const subtextBlock = subtext
          ? `<div class="trace-subtext">${escapeHtml(subtext)}</div>`
          : "";
        return `
          <div class="trace-item ${escapeHtml(event.status)}">
            <div class="trace-tool-row">
              <span class="trace-icon">${traceIcon(event.tool)}</span>
              <span>${escapeHtml(traceToolLabel(event.tool))}</span>
              <span class="trace-chevron">›</span>
            </div>
            <div class="trace-action">
              <span class="trace-dot"></span>
              <div>
                <div>${escapeHtml(action)}</div>
                ${subtextBlock}
              </div>
            </div>
          </div>
        `;
      }).join("");
      return `
        <section class="trace">
          <div class="trace-list">${items}</div>
        </section>
      `;
    }

    function traceActionText(event) {
      if (event.businessName && event.address) {
        if (event.tool.includes("Featherless")) {
          return `Analysiere Satellitenbild fuer ${event.businessName}`;
        }
        if (event.tool.includes("Agent 2") || event.tool.includes("Geocoding")) {
          return `Pruefe Adresse ${event.address}`;
        }
        if (event.tool.includes("Qualification")) {
          return `Bewerte Solar-Lead ${event.businessName}`;
        }
        if (event.tool.includes("Webhook")) {
          return `Bereite Uebergabe an Agent 1 fuer ${event.businessName} vor`;
        }
        return `${event.step}: ${event.businessName}`;
      }
      if (event.tool.includes("Places") || event.tool.includes("Google Places")) {
        return event.detail && event.detail.includes("Kandidaten")
          ? `${event.detail} gefunden`
          : "Suche nach passenden Businesses in der Stadt";
      }
      return event.step;
    }

    function traceToolLabel(tool) {
      if (tool.includes("Places") || tool.includes("web")) return "Searching the web";
      if (tool.includes("Featherless")) return "Analyzing image";
      if (tool.includes("Agent 2") || tool.includes("Geocoding") || tool.includes("Solar")) return "Checking solar potential";
      if (tool.includes("Qualification")) return "Thinking";
      if (tool.includes("Webhook")) return "Sending lead";
      return tool;
    }

    function traceIcon(tool) {
      if (tool.includes("Featherless")) return "◉";
      if (tool.includes("Agent 2") || tool.includes("Geocoding") || tool.includes("Solar")) return "☀";
      if (tool.includes("Qualification")) return "◆";
      if (tool.includes("Webhook")) return "↗";
      return "⌕";
    }

    function renderFinderResult(data) {
      const rows = data.leads.map((lead) => {
        const image = lead.roofImageUrl
          ? `<img class="lead-image" src="${escapeHtml(lead.roofImageUrl)}" alt="Google Maps Satellitenbild fuer ${escapeHtml(lead.businessName)}" />`
          : `<div class="lead-image-missing">Kein Bild verfuegbar</div>`;
        const qualifiedClass = lead.qualified ? "QUALIFIED" : "UNQUALIFIED";
        const qualifiedText = lead.qualified ? "QUALIFIED" : "UNQUALIFIED";
        const website = lead.website
          ? `<a href="${escapeHtml(lead.website)}" target="_blank" rel="noreferrer">${escapeHtml(lead.website)}</a>`
          : "-";
        const maps = lead.googleMapsUrl
          ? `<a href="${escapeHtml(lead.googleMapsUrl)}" target="_blank" rel="noreferrer">Google Maps</a>`
          : "-";
        const rating = lead.rating === null || lead.rating === undefined ? "-" : lead.rating.toFixed(1);
        const agentWarning = lead.agent1Warning
          ? `<p class="lead-reason">${escapeHtml(lead.agent1Warning)}</p>`
          : "";
        const visionWarning = lead.vision.warning
          ? `<p class="lead-reason">${escapeHtml(lead.vision.warning)}</p>`
          : "";

        return `
          <article class="lead-row">
            ${image}
            <div>
              <div class="lead-title">
                <strong>${escapeHtml(lead.businessName)}</strong>
                <span class="pill ${qualifiedClass}">${qualifiedText}</span>
                <span class="pill ${escapeHtml(lead.agent1Status)}">${escapeHtml(lead.agent1Status)}</span>
              </div>
              <div class="lead-meta">
                ${escapeHtml(lead.category)} - ${escapeHtml(lead.address)}<br />
                Telefon: ${escapeHtml(optionalText(lead.phone))} - Website: ${website} - Maps: ${maps} - Rating: ${escapeHtml(rating)}
              </div>
              <div class="lead-stats">
                <span class="pill">${lead.solar.estimatedKwPeak.toFixed(1)} kWp</span>
                <span class="pill">${lead.solar.panelCount} Module</span>
                <span class="pill">Profit ${percent(lead.solar.profitabilityScore)}</span>
                <span class="pill">Vision ${percent(lead.vision.visualSolarPotentialScore)}</span>
              </div>
              <p class="lead-reason">${escapeHtml(lead.qualificationReason)}</p>
              ${visionWarning}
              ${agentWarning}
            </div>
          </article>
        `;
      }).join("");

      result.innerHTML = `
        <div class="decision">
          <div>
            <span class="pill">FINDER</span>
            <strong>${escapeHtml(data.city)}</strong>
          </div>
          <span class="pill">${escapeHtml(data.runId)}</span>
        </div>
        <div class="finder-summary">
          <span class="pill">${data.discoveredCount} gefunden</span>
          <span class="pill QUALIFIED">${data.qualifiedCount} qualifiziert</span>
          <span class="pill SENT">${data.sentToAgent1Count} an Agent 1</span>
        </div>
        ${renderTrace(data.trace)}
        ${rows ? `<div class="lead-list">${rows}</div>` : `<div class="empty">Keine Businesses gefunden.</div>`}
      `;
    }

    function renderResult(data) {
      const warning = data.fallbackWarning
        ? `<div class="warning">${escapeHtml(data.fallbackWarning)}</div>`
        : "";
      const imageSourceLabel = data.roofImageSource;
      const imageMetaLabel = "Satellitenbild";
      const imageBlock = data.roofImageUrl
        ? `<img class="roof-image" src="${escapeHtml(data.roofImageUrl)}" alt="Google Maps Satellitenbild" />
           <div class="image-meta">
             <span>${escapeHtml(imageSourceLabel)}</span>
             <span>${escapeHtml(imageMetaLabel)}</span>
           </div>`
        : `<div class="image-missing">${escapeHtml(data.imageWarning || "Google Maps Bild nicht verfuegbar")}</div>`;
      result.innerHTML = `
        ${warning}
        ${imageBlock}
        <div class="decision">
          <div>
            <span class="pill ${escapeHtml(data.decision)}">${escapeHtml(data.decision)}</span>
            <strong>${escapeHtml(data.nextAction)}</strong>
          </div>
          <span class="pill">${escapeHtml(data.resourceLevel)}</span>
        </div>
        <div class="metrics">
          <div class="metric"><span>Lead Fit</span><b>${percent(data.leadFitScore)}</b></div>
          <div class="metric"><span>Profitabilitaet</span><b>${percent(data.profitabilityScore)}</b></div>
          <div class="metric"><span>Ghosting Risiko</span><b>${percent(data.ghostingRiskScore)}</b></div>
        </div>
        <div class="facts">
          <div class="fact"><span>Adresse</span><b>${escapeHtml(data.formattedAddress)}</b></div>
          <div class="fact"><span>Koordinaten</span><b>${data.latitude.toFixed(4)}, ${data.longitude.toFixed(4)}</b></div>
          <div class="fact"><span>Datenquelle</span><b>${escapeHtml(data.geocodeSource)} / ${escapeHtml(data.solarSource)}</b></div>
          <div class="fact"><span>Solar API Module</span><b>${data.panelCount} x ${Math.round(data.panelCapacityWatts)} W</b></div>
          <div class="fact"><span>PV Leistung</span><b>${data.estimatedKwPeak.toFixed(1)} kWp</b></div>
          <div class="fact"><span>Jahresenergie</span><b>${data.yearlyEnergyKwh.toLocaleString("de-DE")} kWh</b></div>
          <div class="fact"><span>Dach Scores</span><b>${percent(data.roofOrientationScore)} / ${percent(data.roofPitchScore)}</b></div>
          <div class="fact"><span>Ersparnis</span><b>${eur(data.annualSavingsEstimate)} pro Jahr</b></div>
          <div class="fact"><span>Amortisation</span><b>${data.paybackYears.toFixed(1)} Jahre</b></div>
          <div class="fact"><span>Preisrahmen</span><b>${eur(data.estimatedPriceMin)} - ${eur(data.estimatedPriceMax)}</b></div>
          <div class="fact"><span>Lead ID</span><b>${escapeHtml(data.leadId)}</b></div>
          <div class="fact"><span>Rep</span><b>${escapeHtml(data.assignedRep)}</b></div>
        </div>
        <p class="reasoning">${escapeHtml(data.reasoning)}</p>
      `;
    }
  </script>
</body>
</html>
"""
