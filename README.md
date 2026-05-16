# Solar Lead OS - Agentic Revenue Workflow

Solar Lead OS is a standalone Python/FastAPI decision engine for the lablab.ai Agentic Workflows track.

The MVP is deliberately narrow: rich solar intake, profitability decision, automatic email action, hub handoff, offer draft, and a Twilio ConversationRelay call with Gemini as the reasoning engine.

## Features

- Agentic lead form at `GET /`
- Structured intake at `POST /api/intake`
- Full workflow runner at `POST /api/workflows/{lead_id}/run`
- `solar-lead-hub` compatible endpoint at `POST /agent2/evaluate`
- Finder-Agent handoff endpoint at `POST /api/finder/leads`
- Handoff payload at `GET /api/leads/{lead_id}/handoff`
- Offer payload at `GET /api/leads/{lead_id}/offer`
- Generated offer PDF at `GET /api/leads/{lead_id}/offer.pdf`
- Twilio customer call via `POST /intake`, TwiML at `/webhooks/twilio/voice/{lead_id}`, and WebSocket bridge at `/ws/twilio/conversation/{lead_id}`
- Optional Vapi offer demo call at `POST /api/leads/{lead_id}/vapi-offer-call`
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
GOOGLE_SOLAR_API_KEY=
SPEECHMATICS_API_KEY=
VAPI_API_KEY=
VAPI_ASSISTANT_ID=
VAPI_PHONE_NUMBER_ID=
VAPI_CALL_URL=https://api.vapi.ai/call
VAPI_FILE_URL=https://api.vapi.ai/file
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
TWILIO_CALL_URL=https://api.twilio.com/2010-04-01
TWILIO_RELAY_LANGUAGE=multi
TWILIO_RELAY_TTS_PROVIDER=ElevenLabs
TWILIO_RELAY_TRANSCRIPTION_PROVIDER=Deepgram
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
CONVERSATION_SUMMARY_EMAIL=alexander.saade07@gmail.com
```

## Hackathon Demo Flow

1. Open `http://localhost:8000`.
2. Submit the prefilled Anna Becker form.
3. The visible customer page should say that SolarPingu will call shortly; it should not show the internal offer PDF.
4. Twilio calls the form phone number and connects the call to `/ws/twilio/conversation/{lead_id}` through ConversationRelay.
5. Gemini generates the next spoken reply for every caller prompt.
6. For a local real-call test, `PUBLIC_BASE_URL` must be an HTTPS URL that also supports `wss`, for example an ngrok tunnel to port 8000.
7. Test voice Q&A without a call:

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

## Twilio Voice Flow

Twilio places the outbound phone call and uses ConversationRelay for real-time STT/TTS over WebSocket. The local FastAPI WebSocket receives caller prompts, sends the lead, offer, profitability, and recent conversation context to Gemini, then returns text tokens to Twilio for speech playback. For multilingual calls, `TWILIO_RELAY_LANGUAGE=multi` requires Deepgram transcription and ElevenLabs TTS in ConversationRelay.

## Speechmatics Voice Flow

Speechmatics provides the transcript input with German enhanced transcription, speaker diarization, entity metadata, and a solar-specific custom vocabulary. The callback is normalized into a flat transcript, speaker turns, and low-confidence terms. Gemini generates the pitch/Q&A/closing response, and the backend sends a conversation summary with clear next steps to `CONVERSATION_SUMMARY_EMAIL`. Browser speech synthesis or pre-recorded audio can speak the result in the demo; Speechmatics is not used as TTS.

## Vapi Note

Vapi remains optional and is no longer the primary visible demo path. Twilio ConversationRelay is the preferred live-call path; Vapi endpoints are retained for legacy comparisons.

## Tests

```bash
pytest
```
