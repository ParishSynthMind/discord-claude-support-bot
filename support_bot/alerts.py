"""Nothing fails silently: any error in the background work is logged, written to the trail,
posted to the moderators' channel and, if ALERT_WEBHOOK_URL is set, sent there too
(Slack/Discord/n8n style JSON). Alerting itself never raises."""
import logging
from typing import Optional

import httpx

log = logging.getLogger("support_bot.alerts")


class Alerts:
    def __init__(self, store, api=None, mod_channel_id: str = "", webhook_url: str = "",
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.store, self.api, self.mod_channel_id, self.webhook_url = store, api, mod_channel_id, webhook_url
        self.transport = transport
        self.sent: list = []

    async def failure(self, what: str, exc: BaseException) -> None:
        text = f"Support bot problem: {what} failed ({type(exc).__name__}: {str(exc)[:300]})"
        log.error(text)
        self.sent.append(text)
        try:
            self.store.log("failure", what=what, error=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:  # pragma: no cover
            log.exception("could not write the failure to the trail")
        if self.api and self.mod_channel_id:
            try:
                await self.api.send(self.mod_channel_id, text)
            except Exception:
                log.exception("could not post the alert to the mod channel")
        if self.webhook_url:
            try:
                async with httpx.AsyncClient(timeout=10, transport=self.transport) as c:
                    await c.post(self.webhook_url, json={"text": text, "content": text})
            except Exception:
                log.exception("could not post the alert to ALERT_WEBHOOK_URL")
