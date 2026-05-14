# Solar Lead Hub Handoff Schema

FastAPI exposes the rich handoff payload at:

```text
GET /api/leads/{lead_id}/handoff
```

The existing `solar-lead-hub` dashboard can continue to call:

```text
POST /agent2/evaluate
```

## TypeScript Shape

```ts
type HubHandoffPayload = {
  source: "solar-agent-fastapi";
  lead: SolarLeadIntake;
  profitability: ProfitabilityDecision;
  solar_enrichment: Record<string, unknown>;
  offer: OfferDraft;
  demo_url: string;
  created_at: string;
};

type SolarLeadIntake = {
  lead_id: string;
  name: string;
  email: string;
  phone: string;
  address: string;
  owner_status: "owner" | "renter" | "unknown";
  roof_type: "pitched" | "flat" | "unknown";
  need: "cost_savings" | "independence" | "both" | "unknown";
  timeline: "immediate" | "within_3_months" | "within_6_months" | "within_12_months" | "exploring";
  budget_range: "under_10000" | "10000-15000" | "15000-20000" | "20000-30000" | "over_30000" | "unknown";
  decision_maker: string;
  main_concern: string;
  battery_interest: boolean;
  wallbox_interest: boolean;
  preferred_contact: "email" | "phone" | "both";
};
```

## Demo Contract

- `profitability.decision` drives the visible workflow state.
- `offer` is the customer-facing offer draft.
- `demo_url` points to the FastAPI demo payload page.
- `solar_enrichment.source` is either `google_solar_api` or `deterministic_fallback`.
