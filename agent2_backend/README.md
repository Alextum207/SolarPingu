# Solar Lead OS - Agent 2 Backend

FastAPI service for **Agent 2: the Feasibility Ops Agent** plus a
city-based **Finder Agent** for public business lead discovery.

The API is address-first: the client can send only a postal address. Agent 2 geocodes the address, looks up Google Solar `buildingInsights`, calculates rooftop solar and financial potential, scores the lead, and returns a structured `projectdecision.json`.

The service is hackathon-safe: if Geocoding, Solar API, or Gemini fail, deterministic fallback logic still returns valid demo JSON.

## Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS/Linux:

```bash
source .venv/bin/activate
```

## Run Locally

```bash
uvicorn app.main:app --reload --port 8000
```

Open:

- `http://localhost:8000/`
- `http://localhost:8000/docs`
- `http://localhost:8000/health`
- `http://localhost:8000/agent2/sample-response`

## Finder Agent Workflow

1. `POST /finder/run` receives only a city.
2. `PlacesService` searches Google Places for configured commercial categories:
   Autohaus, Logistik, Lagerhalle, Produktion, Grosshandel, Baumarkt,
   Supermarkt, Fitnessstudio, Moebelhaus, and Gewerbepark.
3. Each business address is evaluated through Agent 2.
4. `FeatherlessVisionService` analyzes the Google Maps Static satellite image
   when a Featherless key and roof image are available.
5. Qualified leads are sent to Agent 1 with a webhook POST.

If Google Places, Featherless, Maps Static, or Agent 1 are unavailable, the
service remains demo-safe: mock businesses are used, Agent 2 still evaluates
addresses, vision failures are marked with `vision.warning`, and webhook sends
are skipped with an explicit status.

## Finder Curl

```bash
curl -X POST "http://localhost:8000/finder/run" ^
  -H "Content-Type: application/json" ^
  -d "{\"city\":\"Frankfurt am Main\"}"
```

macOS/Linux:

```bash
curl -X POST "http://localhost:8000/finder/run" \
  -H "Content-Type: application/json" \
  -d '{"city":"Frankfurt am Main"}'
```

## Address-First Workflow

1. `POST /agent2/evaluate` receives an address and optional lead metadata.
2. `GeocodingService` calls Google Geocoding API and returns formatted address, latitude, longitude, and place ID.
3. `SolarService` calls Google Solar API `buildingInsights:findClosest` using latitude and longitude.
4. `ScoringService` calculates PV size, annual energy, savings, payback, lead fit, profitability, and ghosting risk.
5. `SolarService` requests a real Google Maps Static satellite image and exposes it through a local `roofImageUrl`. The API key stays server-side.
6. `DecisionService` uses Gemini as an optional final structured decision layer. If Gemini is unavailable or invalid, deterministic rules choose `PURSUE`, `NURTURE`, or `REJECT`.

## Minimal Curl

```bash
curl -X POST "http://localhost:8000/agent2/evaluate" ^
  -H "Content-Type: application/json" ^
  -d "{\"address\":\"Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany\"}"
```

macOS/Linux:

```bash
curl -X POST "http://localhost:8000/agent2/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"address":"Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany"}'
```

## Full Demo Request

```bash
curl -X POST "http://localhost:8000/agent2/evaluate" ^
  -H "Content-Type: application/json" ^
  -d @sample_data/address_request.sample.json
```

```json
{
  "leadId": "L-001",
  "address": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
  "name": "Anna Becker",
  "batteryInterest": true,
  "budgetRange": "15000-20000",
  "installationTimeline": "within_3_months",
  "ownerStatus": "owner",
  "objections": ["financing uncertainty"]
}
```

Only `address` is required. Missing metadata defaults to safe values and the service still returns a feasibility decision.

## Example Response

```json
{
  "leadId": "L-001",
  "inputAddress": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
  "formattedAddress": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany",
  "latitude": 50.160936,
  "longitude": 8.486981,
  "placeId": "ChIJn-S8N4imvUcRGloQiGcQfCg",
  "geocodeSource": "GOOGLE_GEOCODING_API",
  "solarSource": "GOOGLE_SOLAR_API",
  "fallbackWarning": null,
  "roofImageUrl": "/agent2/roof-image/L-001-f50b0b93c3.png",
  "roofImageSource": "GOOGLE_MAPS_STATIC",
  "imageryDate": null,
  "imageWarning": null,
  "panelCount": 29,
  "panelCapacityWatts": 400,
  "estimatedKwPeak": 11.6,
  "yearlyEnergyKwh": 10675,
  "roofOrientationScore": 0.56,
  "roofPitchScore": 0.93,
  "annualSavingsEstimate": 2380,
  "paybackYears": 10.0,
  "estimatedPriceMin": 20800,
  "estimatedPriceMax": 26700,
  "leadFitScore": 0.84,
  "profitabilityScore": 0.82,
  "ghostingRiskScore": 0.27,
  "decision": "PURSUE",
  "resourceLevel": "MEDIUM_TOUCH",
  "nextAction": "GENERATE_OFFER_AND_SCHEDULE_CONSULTATION",
  "assignedRep": "Sales Rep 1",
  "reasoning": "Usable rooftop solar potential, good estimated annual energy, owner-occupied property, short buying timeline."
}
```

## Fallback Behavior

By default, `.env.example` uses:

```env
USE_MOCK_GEOCODING=true
USE_MOCK_SOLAR=true
```

That means:

- Geocoding returns deterministic demo coordinates from the address.
- Solar enrichment returns deterministic rooftop potential from the coordinates.
- Scores and financials are always computed in Python.
- Gemini is optional and never required for a successful response.
- A real Google Maps satellite image is returned through `roofImageUrl` when Maps Static API is enabled. Agent 2 does not expose the API key to the browser; it proxies the image from the backend. If Maps Static API is unavailable, `roofImageUrl` is `null` and `imageWarning` explains what must be enabled.
- No panel layout image is generated or simulated. The UI shows only the unmodified Google Maps satellite image.

## Agent Integration Contract

For another agent, call only:

```http
POST /agent2/evaluate
Content-Type: application/json
```

Minimum body:

```json
{"address": "Am Schnittelberg 14, 65812 Bad Soden am Taunus, Germany"}
```

The endpoint returns `200` with structured JSON for every valid address, using fallbacks when upstream APIs fail. Invalid client input returns `422`. Use `fallbackWarning`, `geocodeSource`, `solarSource`, and `roofImageSource` to decide how much trust to place in the result.

To use real Google APIs:

```env
USE_MOCK_GEOCODING=false
USE_MOCK_SOLAR=false
GOOGLE_GEOCODING_API_KEY=your_google_maps_platform_key
GOOGLE_SOLAR_API_KEY=your_google_maps_platform_key
GOOGLE_MAPS_STATIC_API_KEY=your_google_maps_platform_key
GOOGLE_PLACES_API_KEY=your_google_maps_platform_key
FEATHERLESS_API_KEY=your_featherless_key
AGENT1_WEBHOOK_URL=https://agent1.example.com/leads
```

The Geocoding API turns address into coordinates. The Solar API uses those coordinates with `buildingInsights:findClosest`. Any upstream failure falls back to deterministic demo data.

If the returned `geocodeSource` is `MOCK`, the coordinates are demo fallback coordinates. For real address-accurate coordinates, enable **Geocoding API** in the same Google Cloud project as the API key and keep `USE_MOCK_GEOCODING=false`.
For real satellite roof images, also enable **Maps Static API** in the same Google Cloud project.
For real business discovery, enable **Places API (New)** and keep
`USE_MOCK_PLACES=false`.

## Project Shape

```text
backend/
  app/
    main.py
    models.py
    config.py
    services/
      agent1.py
      evaluation.py
      finder.py
      geocoding.py
      places.py
      solar.py
      scoring.py
      decision.py
      vision.py
  tests/
    test_finder_api.py
    test_finder_services.py
  sample_data/
    address_request.sample.json
    projectdecision.sample.json
```
