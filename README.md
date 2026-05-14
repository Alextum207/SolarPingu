# SolarPingu - Agent 1 Qualification App

SolarPingu replaces the Agent 1 n8n workflow with a standalone Python/FastAPI app.

The app accepts warm solar leads, shows free Google Calendar slots, books a qualification call, creates a Gemini call plan, submits later call recordings to Speechmatics, and stores the structured qualification result in SQLite.

## Features

- Public lead form at `GET /`
- Free-slot API at `GET /api/slots`
- Lead creation at `POST /api/leads`
- Recording submission at `POST /api/recordings`
- Speechmatics callback at `POST /webhooks/speechmatics`
- Health check at `GET /health`
- SQLite persistence in `data/solar_agent.db`
- Local fallback mode when external API keys are not configured

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000`.

## Environment

```bash
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
SPEECHMATICS_API_KEY=
GOOGLE_CALENDAR_ID=primary
GOOGLE_APPLICATION_CREDENTIALS=
PUBLIC_BASE_URL=https://your-domain.example
DATABASE_URL=sqlite:///data/solar_agent.db
APP_TIMEZONE=Europe/Berlin
```

`GOOGLE_APPLICATION_CREDENTIALS` should point to a Google service account JSON file that has access to the target calendar. If it is empty, the app runs in local mode and simulates calendar availability/bookings.

## Recording Flow

After a call, submit either a public recording URL:

```bash
curl -X POST http://localhost:8000/api/recordings ^
  -F lead_id=SL_EXAMPLE ^
  -F recording_url=https://example.com/call.wav
```

Or upload an audio file:

```bash
curl -X POST http://localhost:8000/api/recordings ^
  -F lead_id=SL_EXAMPLE ^
  -F audio_file=@call.wav
```

Speechmatics calls back to:

```text
{PUBLIC_BASE_URL}/webhooks/speechmatics
```

## Vultr Deployment Notes

Run the app behind HTTPS and set `PUBLIC_BASE_URL` to the public domain so Speechmatics can reach the callback. A simple production command is:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For a longer-running deployment, put it behind systemd, Docker, or a process manager, and mount `data/` plus `.env` outside the image.

## Tests

```bash
pytest
```
