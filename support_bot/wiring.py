"""Builds the bot from Settings. Tests and the local run pass fakes in; a real run uses
Discord, Claude and the Keychain."""
from typing import Optional

import httpx

from .ai import ClaudeAI
from .alerts import Alerts
from .discord_api import DiscordAPI
from .handlers import SupportBot
from .kb import KnowledgeBase
from .moderation import Screener
from .store import Store


def build(settings, ai=None, transport: Optional[httpx.AsyncBaseTransport] = None, store: Optional[Store] = None,
          alert_transport: Optional[httpx.AsyncBaseTransport] = None) -> SupportBot:
    store = store or Store(settings.db_path)
    api = DiscordAPI(settings.bot_token, settings.api_base, transport=transport)
    alerts = Alerts(store, api, settings.mod_channel_id, settings.alert_webhook_url, transport=alert_transport)
    if ai is None:
        ai = ClaudeAI(settings.anthropic_api_key, settings.model, settings.community, settings.max_output_tokens)
    kb = KnowledgeBase.load(settings.kb_dir)
    screener = Screener() if settings.enable_gateway else None
    return SupportBot(settings, store, kb, ai, api, alerts, screener)
