import asyncio

import pytest

from support_bot.ai import FakeAI
from support_bot.gateway import MESSAGE_CONTENT, Gateway, GatewayFatal, intents
from support_bot.mock_discord import FakeGateway
from support_bot.moderation import Screener

from .conftest import ASKER, MOD, MOD_PERMS, OTHER, make_world, msgs


def spam(w, content="Free nitro here discord.gg/abc", author=OTHER):
    return w.fake.add_message(w.general, author, content, guild_id=w.fake.guild_id)


async def report(w, target, user=ASKER):
    out = await w.side.send(w.side.message_command("Report to moderators", user, target))
    await w.bot.drain()
    return out


async def test_member_report_reaches_mod_queue_with_label_and_buttons(world):
    target = spam(world)
    _, data, _ = await report(world, target)
    assert data["data"]["flags"] == 64 and "Nothing is removed" in data["data"]["content"]
    [q] = msgs(world, world.mod)
    assert "Report #1" in q["content"] and target["id"] in q["content"]
    assert "AI label (a suggestion only): spam, medium" in q["content"]
    assert [b["label"] for b in q["components"][0]["components"]] == ["Dismiss", "Remove message"]
    assert q["allowed_mentions"] == {"parse": []}


async def test_reporting_never_deletes_by_itself(world):
    target = spam(world)
    await report(world, target)
    assert target["id"] in world.fake.messages[world.general]
    assert not any(c[0] == "DELETE" for c in world.fake.calls)


async def test_moderator_removes_after_pressing_the_button(world):
    target = spam(world)
    await report(world, target)
    q = msgs(world, world.mod)[0]
    _, data, _ = await world.side.send(world.side.button("remove:1", MOD, q, MOD_PERMS))
    await world.bot.drain()
    assert data["type"] == 7 and "Removed by" in data["data"]["content"]
    assert target["id"] not in world.fake.messages[world.general]
    assert world.store.report(1)["status"] == "removed" and world.store.report(1)["resolved_by"] == MOD
    q_after = world.fake.messages[world.mod][q["id"]]   # Discord applies UPDATE_MESSAGE to the clicked message
    assert q_after["components"] == [] and "Removed by" in q_after["content"]


async def test_non_moderator_cannot_remove(world):
    target = spam(world)
    await report(world, target)
    q = msgs(world, world.mod)[0]
    _, data, _ = await world.side.send(world.side.button("remove:1", ASKER, q))
    await world.bot.drain()
    assert "Only moderators" in data["data"]["content"]
    assert target["id"] in world.fake.messages[world.general]


async def test_dismiss(world):
    await report(world, spam(world))
    q = msgs(world, world.mod)[0]
    _, data, _ = await world.side.send(world.side.button("dismiss:1", MOD, q, MOD_PERMS))
    assert "Dismissed" in data["data"]["content"] and world.store.report(1)["status"] == "dismissed"
    _, again, _ = await world.side.send(world.side.button("remove:1", MOD, q, MOD_PERMS))
    assert "already handled" in again["data"]["content"]


async def test_already_deleted_message_is_fine(world):
    target = spam(world)
    await report(world, target)
    del world.fake.messages[world.general][target["id"]]
    q = msgs(world, world.mod)[0]
    await world.side.send(world.side.button("remove:1", MOD, q, MOD_PERMS))
    await world.bot.drain()
    assert not any("Support bot problem" in m["content"] for m in msgs(world, world.mod))


async def test_failed_delete_reopens_report_and_alerts(world):
    target = spam(world)
    await report(world, target)
    world.fake.fail_next["delete"] = 403
    q = msgs(world, world.mod)[0]
    await world.side.send(world.side.button("remove:1", MOD, q, MOD_PERMS))
    await world.bot.drain()
    assert world.store.report(1)["status"] == "open"
    assert any("Support bot problem" in m["content"] for m in msgs(world, world.mod))


async def test_same_member_reporting_twice(world):
    target = spam(world)
    await report(world, target)
    _, data, _ = await report(world, target)
    assert "already reported" in data["data"]["content"] and len(msgs(world, world.mod)) == 1


async def test_second_reporter_shows_count(world):
    target = spam(world)
    await report(world, target)
    await report(world, target, user="1400000000000000003")
    assert "(2 open reports on this message)" in msgs(world, world.mod)[1]["content"]


async def test_report_rate_limit():
    w = make_world(reports_per_user=1)
    await report(w, spam(w, "one"))
    _, data, _ = await report(w, spam(w, "two"))
    assert "lot of reports" in data["data"]["content"]


async def test_bot_messages_cannot_be_reported(world):
    target = world.fake.add_message(world.general, world.fake.application_id, "an answer", guild_id=world.fake.guild_id)
    _, data, _ = await report(world, target)
    assert "bot message" in data["data"]["content"]


async def test_triage_respects_spend_cap():
    w = make_world(daily_spend_cap_usd=0.0)
    await report(w, spam(w))
    assert w.ai.calls == [] and "AI label" not in msgs(w, w.mod)[0]["content"]


async def test_triage_failure_still_posts_report():
    w = make_world(ai=FakeAI(triages=[RuntimeError("boom")]))
    await report(w, spam(w))
    texts = [m["content"] for m in msgs(w, w.mod)]
    assert any("Report #1" in t for t in texts) and any("Support bot problem" in t for t in texts)


async def test_reported_text_cannot_ping_mods(world):
    await report(world, spam(world, "hey @everyone <@&1300000000000000777> look"))
    assert "@everyone" not in msgs(world, world.mod)[0]["content"]


# screener rules ------------------------------------------------------------
def m(content="", **kw):
    return {"id": "1", "guild_id": "g", "channel_id": "c", "author": {"id": "u"}, "content": content,
            "mentions": [], "mention_roles": [], "mention_everyone": False, **kw}


@pytest.mark.parametrize("msg,expect", [
    (m("join discord.gg/xyz"), "invite"),
    (m("FREE NITRO for all"), "blocked phrase"),
    (m("", mention_everyone=True), "@everyone"),
    (m("", mentions=[{"id": str(i)} for i in range(5)]), "mentions 5"),
    (m("hello there"), None),
    (m("discord.gg/xyz", author={"id": "b", "bot": True}), None),
])
def test_screener_rules(msg, expect):
    why = Screener().screen(msg)
    assert (why is None) if expect is None else (expect in why)


def test_screener_repeat_rule():
    s = Screener(repeat_count=3)
    assert [s.screen(m("buy my course")) for _ in range(3)][-1].startswith("repeated")


def test_intents():
    assert intents(False) == (1 << 0) | (1 << 9)
    assert intents(True) & MESSAGE_CONTENT


# gateway over a real websocket -----------------------------------------------
async def run_gateway(fg, token, content_intent, w):
    url = await fg.start()
    gw = Gateway(token, url, w.bot.on_gateway_event, content_intent, max_reconnects=0)
    task = asyncio.create_task(gw.run())
    return gw, task


async def test_gateway_flags_spam_without_deleting():
    w = make_world(enable_gateway=True)
    w.bot.screener = Screener()
    fg = FakeGateway(allow_message_content=True, heartbeat_ms=50)
    gw, task = await run_gateway(fg, w.fake.bot_token, True, w)
    await asyncio.wait_for(gw.ready.wait(), 5)
    msg = spam(w, "free nitro discord.gg/scam")
    await fg.dispatch_message(msg)
    await asyncio.sleep(0.2)
    await w.bot.drain()
    gw.stop()
    task.cancel()
    await fg.stop()
    assert fg.identified[0]["intents"] & MESSAGE_CONTENT and fg.heartbeats >= 1
    [q] = msgs(w, w.mod)
    assert "Automatic rule" in q["content"] and w.store.report(1)["source"] == "gateway"
    assert msg["id"] in w.fake.messages[w.general]


async def test_gateway_without_content_intent_still_catches_mass_mentions():
    w = make_world(enable_gateway=True)
    w.bot.screener = Screener()
    fg = FakeGateway(heartbeat_ms=50)
    gw, task = await run_gateway(fg, w.fake.bot_token, False, w)
    await asyncio.wait_for(gw.ready.wait(), 5)
    await fg.dispatch_message(spam(w, "free nitro"))   # content is blanked: not flagged
    await fg.dispatch_message(dict(spam(w, "hi"), mentions=[{"id": str(i)} for i in range(6)]))
    await asyncio.sleep(0.2)
    await w.bot.drain()
    gw.stop()
    task.cancel()
    await fg.stop()
    assert len(w.store.reports()) == 1 and "mentions 6" in w.store.reports()[0]["reason"]


@pytest.mark.parametrize("token,content,code", [("wrong", False, 4004), ("test-bot-token", True, 4014)])
async def test_gateway_fatal_close_codes(token, content, code):
    w = make_world()
    fg = FakeGateway(allow_message_content=False)
    url = await fg.start()
    gw = Gateway(token, url, w.bot.on_gateway_message, content, max_reconnects=0)
    with pytest.raises(GatewayFatal, match=str(code)):
        await asyncio.wait_for(gw.run(), 5)
    await fg.stop()


async def test_slow_label_does_not_block_the_gateway_and_is_not_lost():
    """Regression: the first real run awaited the Claude label inside the websocket loop, so
    heartbeats stalled and work was lost on shutdown."""
    class SlowAI(FakeAI):
        async def triage(self, content, reason):
            await asyncio.sleep(0.6)
            return await super().triage(content, reason)
    w = make_world(ai=SlowAI(), enable_gateway=True)
    w.bot.screener = Screener()
    fg = FakeGateway(allow_message_content=True, heartbeat_ms=100)
    gw, task = await run_gateway(fg, w.fake.bot_token, True, w)
    await asyncio.wait_for(gw.ready.wait(), 5)
    await fg.dispatch_message(spam(w, "free nitro discord.gg/one"))
    await fg.dispatch_message(spam(w, "crypto giveaway, DM me", author="1400000000000000005"))
    beats_before = fg.heartbeats
    await asyncio.sleep(0.4)            # both labels still running
    assert fg.heartbeats > beats_before  # the loop kept heartbeating
    gw.stop()
    task.cancel()
    await w.bot.drain()                  # nothing lost even though the connection is gone
    await fg.stop()
    assert len(w.store.reports()) == 2 and len(msgs(w, w.mod)) == 2
