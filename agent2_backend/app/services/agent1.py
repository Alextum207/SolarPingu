import asyncio
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import Agent1DeliveryResult, Agent1DeliveryStatus


class Agent1WebhookService:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

    async def send_lead(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> Agent1DeliveryResult:
        if not self.settings.agent1_webhook_url:
            return Agent1DeliveryResult(
                status=Agent1DeliveryStatus.SKIPPED,
                sent=False,
                warning="AGENT1_WEBHOOK_URL missing; lead not sent",
            )

        last_warning = "Agent 1 webhook failed"
        timeout = httpx.Timeout(self.settings.external_api_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            for attempt in range(1, self.settings.agent1_webhook_max_attempts + 1):
                try:
                    response = await client.post(
                        self.settings.agent1_webhook_url,
                        headers={
                            "Content-Type": "application/json",
                            "Idempotency-Key": idempotency_key,
                        },
                        json=payload,
                    )
                    if response.is_success:
                        return Agent1DeliveryResult(
                            status=Agent1DeliveryStatus.SENT,
                            sent=True,
                            statusCode=response.status_code,
                        )
                    last_warning = (
                        f"Agent 1 webhook returned HTTP {response.status_code}"
                    )
                except httpx.HTTPError as exc:
                    last_warning = f"Agent 1 webhook request failed: {exc.__class__.__name__}"

                if attempt < self.settings.agent1_webhook_max_attempts:
                    await asyncio.sleep(0.2 * attempt)

        return Agent1DeliveryResult(
            status=Agent1DeliveryStatus.FAILED,
            sent=False,
            warning=last_warning,
        )
