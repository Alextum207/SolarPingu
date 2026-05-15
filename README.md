# Solar Lead OS - Agentic Revenue Workflow

Solar Lead OS is a standalone Python/FastAPI decision engine for the lablab.ai Agentic Workflows track.

The MVP is deliberately narrow: rich solar intake, profitability decision, automatic email action, hub handoff, offer draft, and Speechmatics/Gemini voice Q&A.

## Features

- Agentic lead form at `GET /`
- Structured intake at `POST /api/intake`
- Full workflow runner at `POST /api/workflows/{lead_id}/run`
- `solar-lead-hub` compatible endpoint at `POST /agent2/evaluate`
- Finder-Agent handoff endpoint at `POST /api/finder/leads`
- Handoff payload at `GET /api/leads/{lead_id}/handoff`
- Offer payload at `GET /api/leads/{lead_id}/offer`
- Generated offer PDF at `GET /api/leads/{lead_id}/offer.pdf`
- Vapi offer demo call at `POST /api/leads/{lead_id}/vapi-offer-call`
- Voice session at `POST /api/voice/session`
- Speechmatics callback at `POST /webhooks/speechmatics`
- Vapi callback at `POST /webhooks/vapi`
- SQLite persistence in `data/solar_agent.db`
- Stable fallback mode when API credentials are missing

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000`.

## Finder Agent / Agent 2

The repository also includes a separate Finder/Agent-2 service in
`agent2_backend/`. It can search Google Places for businesses in a city,
evaluate rooftop potential with Google Solar, inspect the satellite image with
Featherless Vision, and send qualified leads into Agent 1.

Frontend/control-board integrations can talk only to Agent 1:

- `POST /api/finder/run` proxies `{ "city": "Frankfurt am Main" }` to Agent 2.
- `GET /api/finder/leads` lists Finder leads received by Agent 1.
- `POST /api/finder/leads` is the Agent-2 webhook target.

Run Agent 1:

```bash
uvicorn app.main:app --reload --port 8000
```

Run Finder/Agent 2 in a second terminal:

```bash
cd agent2_backend
copy .env.example .env
uvicorn app.main:app --reload --port 8001
```

For local handoff from Finder to Agent 1, set this in `agent2_backend/.env`:

```bash
AGENT1_WEBHOOK_URL=http://127.0.0.1:8000/api/finder/leads
```

For frontend-to-Agent-2 proxying through Agent 1, set this in the root `.env`:

```bash
AGENT2_BASE_URL=http://127.0.0.1:8001
```

Open Finder/Agent 2 at `http://127.0.0.1:8001/`.

## Environment

```bash
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
FEATHERLESS_API_KEY=
FEATHERLESS_BASE_URL=https://api.featherless.ai/v1
FEATHERLESS_MODEL=google/gemma-3-27b-it
GOOGLE_SOLAR_API_KEY=
SPEECHMATICS_API_KEY=
VAPI_API_KEY=
VAPI_ASSISTANT_ID=
VAPI_PHONE_NUMBER_ID=
VAPI_CALL_URL=https://api.vapi.ai/call
GOOGLE_CALENDAR_ID=primary
GOOGLE_APPLICATION_CREDENTIALS=
PUBLIC_BASE_URL=https://your-domain.example
BOOKING_BASE_URL=
DATABASE_URL=sqlite:///data/solar_agent.db
APP_TIMEZONE=Europe/Berlin
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
FROM_EMAIL=solar-lead-os@example.com
STAFF_NOTIFY_EMAIL=sales-team@example.com
```

## Hackathon Demo Flow

1. Open `http://localhost:8000`.
2. Submit the prefilled Anna Becker form.
3. Show the decision: `PURSUE`, score, reasons, offer range, email action.
4. Show the embedded offer PDF and open `/api/leads/{lead_id}/offer.pdf`.
5. Use the Vapi button to test a phone demo about the offer.
5. Test voice Q&A:

```bash
curl -X POST http://localhost:8000/api/voice/session ^
  -H "Content-Type: application/json" ^
  -d "{\"lead_id\":\"SL_EXAMPLE\",\"prompt\":\"Der Preis klingt teuer. Lohnt sich das wirklich?\"}"
```

For a bad-fit lead, post `demo_fixtures/reject_lead.json` to `/api/intake` and run the workflow. It must reject and avoid sending a booking email.

## Profitability Logic

The score combines owner status, budget fit, timeline urgency, roof/solar potential, battery/wallbox upsell, decision-maker clarity, and hard disqualifiers. `PURSUE` requires score >= 70, owner status, plausible budget/timeline, and no hard disqualifier.

## Hub Integration

See [docs/handoff_schema.md](docs/handoff_schema.md). The current `solar-lead-hub` can keep calling `POST /agent2/evaluate`; richer demos can read `GET /api/leads/{lead_id}/handoff`.

## Speechmatics Voice Flow

Speechmatics provides the transcript input. Gemini generates the pitch/Q&A/closing response. Browser speech synthesis or pre-recorded audio can speak the result in the demo; Speechmatics is not used as TTS.

## Vapi Note

Vapi remains optional. Free Vapi numbers may not call German numbers, so the winning demo should not depend on live outbound calling.

## Tests

```bash
pytest
```
