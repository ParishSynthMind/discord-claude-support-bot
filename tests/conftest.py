from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from support_bot.ai import FakeAI
from support_bot.app import create_app
from support_bot.config import Settings
from support_bot.mock_discord import DiscordSide, FakeDiscord
from support_bot.signature import Verifier
from support_bot.store import Store
from support_bot.wiring import build

KB = str(Path(__file__).resolve().parent.parent / "knowledge_base")
MOD_PERMS = (1 << 13) | (1 << 34)   # Manage Messages + Manage Threads
ASKER, OTHER, MOD = "1400000000000000001", "1400000000000000002", "1400000000000000099"


def make_world(ai=None, **overrides):
    fake = FakeDiscord()
    support = fake.add_channel("support")
    mod = fake.add_channel("mod-queue")
    general = fake.add_channel("general")
    side = DiscordSide(fake)
    settings = Settings(application_id=fake.application_id, public_key=side.public_key, bot_token=fake.bot_token,
                        anthropic_api_key=None, guild_id=fake.guild_id, support_channel_id=support,
                        mod_channel_id=mod, mod_role_id="1300000000000000777", kb_dir=KB, db_path=":memory:",
                        api_base="http://discord/api/v10", daily_spend_cap_usd=1.0)
    for k, v in overrides.items():
        setattr(settings, k, v)
    ai = ai or FakeAI()
    bot = build(settings, ai=ai, transport=fake.transport(), store=Store(":memory:"))
    bot.api.sleep = _no_sleep
    app = create_app(bot, Verifier(settings.public_key, settings.signature_max_age_s))
    side.transport = httpx.ASGITransport(app=app)
    return SimpleNamespace(fake=fake, side=side, bot=bot, ai=ai, settings=settings, store=bot.store,
                           support=support, mod=mod, general=general, app=app)


async def _no_sleep(_s):
    return None


@pytest.fixture
def world():
    return make_world()


async def ask(w, question, user=ASKER, perms=0):
    status, data, secs = await w.side.send(w.side.slash("support", user, w.general, perms, question=question))
    await w.bot.drain()
    return status, data, secs


def threads(w):
    return [c for c in w.fake.channels.values() if c.get("parent_id")]


def msgs(w, channel_id):
    return w.fake.thread_messages(channel_id)
