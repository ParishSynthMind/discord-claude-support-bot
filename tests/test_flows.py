"""End to end: signed interactions from the Discord side, through the bot's HTTP endpoint,
into the fake Discord REST API."""
import time

from support_bot.ai import FakeAI

from .conftest import ASKER, MOD, MOD_PERMS, OTHER, ask, make_world, msgs, threads


async def test_endpoint_check_like_discord_does(world):
    result = await world.side.endpoint_check()
    assert result == {"ping_ok": True, "bad_signature_refused": True, "missing_signature_refused": True}


async def test_tampered_body_is_401_and_nothing_happens(world):
    p = world.side.slash("support", ASKER, world.general, question="refund?")
    import json
    body = json.dumps(p).encode()
    ts = str(int(time.time()))
    from support_bot.signature import sign
    sig = sign(world.side.key, ts, body)
    status, _, _ = await world.side.send(p, signature=sig, timestamp=ts, raw=body.replace(b"refund", b"refunx"))
    assert status == 401 and world.store.cases() == []


async def test_replayed_old_request_is_401(world):
    p = world.side.slash("support", ASKER, world.general, question="refund?")
    status, _, _ = await world.side.send(p, timestamp=str(int(time.time()) - 3600))
    assert status == 401


async def test_support_answers_in_a_thread_with_sources(world):
    status, data, secs = await ask(world, "Can I get a refund on my annual plan?")
    assert status == 200 and data == {"type": 5} and secs < 3
    [thread] = threads(world)
    assert thread["type"] == 11 and thread["name"].startswith("Case 1:")
    texts = [m["content"] for m in msgs(world, thread["id"])]
    assert texts[0].startswith(f"<@{ASKER}> asked:")
    assert "-# Sources: billing.md > Refunds" in texts[1]
    labels = [b["label"] for b in msgs(world, thread["id"])[1]["components"][0]["components"]]
    assert labels == ["Solved", "Talk to a human"]
    original = [m for m in msgs(world, world.general)]
    assert original and f"<#{thread['id']}>" in original[0]["content"]
    assert world.store.case(1)["answer_status"] == "answered"


async def test_only_the_asker_is_pinged_into_the_thread(world):
    await ask(world, "refund annual plan")
    first = msgs(world, threads(world)[0]["id"])[0]
    assert first["allowed_mentions"] == {"users": [ASKER]}
    assert all(m["allowed_mentions"] == {"parse": []} for m in msgs(world, threads(world)[0]["id"])[1:])


async def test_question_not_in_docs_makes_no_model_call_and_offers_a_human(world):
    await ask(world, "What's the best sourdough bread recipe?")
    assert world.ai.calls == []
    last = msgs(world, threads(world)[0]["id"])[-1]
    assert "can't find that in our help docs" in last["content"]
    assert last["components"][0]["components"][0]["custom_id"] == "human:1"
    assert world.store.spend_today()["calls"] == 0


async def test_model_says_not_in_docs(world):
    world.ai.answers.append({"status": "not_in_docs", "answer": "The docs don't cover that.", "citations": []})
    await ask(world, "Does Lumen have a Linux app for sync?")
    assert world.store.case(1)["answer_status"] == "not_in_docs"
    assert "Talk to a human" in msgs(world, threads(world)[0]["id"])[-1]["content"]


async def test_needs_human_hands_over_automatically(world):
    world.ai.answers.append({"status": "needs_human", "answer": "A moderator will help with your charge.", "citations": []})
    await ask(world, "I was charged twice for Pro, refund me")
    assert world.store.case(1)["status"] == "handed_over"
    [queue] = msgs(world, world.mod)
    assert "Case #1 needs a person" in queue["content"]
    assert queue["allowed_mentions"] == {"roles": [world.settings.mod_role_id]}


async def test_model_output_cannot_ping_anyone(world):
    world.ai.answers.append({"status": "answered", "answer": "Sure @everyone <@&1> refunds in 14 days", "citations": [1]})
    await ask(world, "refund annual plan")
    answer = msgs(world, threads(world)[0]["id"])[1]["content"]
    assert "@everyone" not in answer and "<@&1>" not in answer


async def test_talk_to_a_human_button(world):
    await ask(world, "refund annual plan")
    answer_msg = msgs(world, threads(world)[0]["id"])[1]
    status, data, _ = await world.side.send(world.side.button("human:1", ASKER, answer_msg))
    await world.bot.drain()
    assert data["data"]["flags"] == 64 and "moderator has been asked" in data["data"]["content"]
    assert world.store.case(1)["status"] == "handed_over"
    assert len(msgs(world, world.mod)) == 1
    # pressing again does not ping the mods twice
    _, data2, _ = await world.side.send(world.side.button("human:1", ASKER, answer_msg))
    await world.bot.drain()
    assert "already with a moderator" in data2["data"]["content"] and len(msgs(world, world.mod)) == 1


async def test_someone_else_cannot_take_over_your_case(world):
    await ask(world, "refund annual plan")
    answer_msg = msgs(world, threads(world)[0]["id"])[1]
    _, data, _ = await world.side.send(world.side.button("human:1", OTHER, answer_msg))
    _, data2, _ = await world.side.send(world.side.button("solved:1", OTHER, answer_msg))
    assert "Only the person who asked" in data["data"]["content"]
    assert "Only the person who asked" in data2["data"]["content"]
    assert world.store.case(1)["status"] == "open"


async def test_human_slash_command_inside_the_thread(world):
    await ask(world, "refund annual plan")
    tid = threads(world)[0]["id"]
    _, data, _ = await world.side.send(world.side.slash("human", ASKER, tid))
    await world.bot.drain()
    assert world.store.case(1)["status"] == "handed_over"
    _, outside, _ = await world.side.send(world.side.slash("human", ASKER, world.general))
    assert "inside one of your support case threads" in outside["data"]["content"]


async def test_solved_updates_message_and_archives_thread(world):
    await ask(world, "refund annual plan")
    tid = threads(world)[0]["id"]
    answer_msg = msgs(world, tid)[1]
    _, data, _ = await world.side.send(world.side.button("solved:1", ASKER, answer_msg))
    await world.bot.drain()
    assert data["type"] == 7 and data["data"]["components"] == [] and "Marked solved" in data["data"]["content"]
    assert world.fake.channels[tid]["archived"] is True
    assert world.store.case(1)["status"] == "closed"


async def test_close_needs_moderator_permission(world):
    await ask(world, "refund annual plan")
    tid = threads(world)[0]["id"]
    _, denied, _ = await world.side.send(world.side.slash("close", ASKER, tid))
    assert "Only moderators" in denied["data"]["content"]
    _, ok, _ = await world.side.send(world.side.slash("close", MOD, tid, MOD_PERMS))
    await world.bot.drain()
    assert "closed by" in ok["data"]["content"] and "flags" not in ok["data"]
    assert world.fake.channels[tid]["archived"] is True


async def test_close_from_mod_queue_button(world):
    world.ai.answers.append({"status": "needs_human", "answer": "A moderator will help.", "citations": []})
    await ask(world, "my account is locked, help")
    queue_msg = msgs(world, world.mod)[0]
    _, data, _ = await world.side.send(world.side.button("close:1", MOD, queue_msg, MOD_PERMS))
    await world.bot.drain()
    assert data["type"] == 7 and world.store.case(1)["status"] == "closed"
    assert "A moderator closed this case" in msgs(world, threads(world)[0]["id"])[-1]["content"]


async def test_per_user_rate_limit(world):
    world.settings.support_per_user = 2
    for _ in range(2):
        await ask(world, "refund annual plan")
    status, data, _ = await ask(world, "refund annual plan")
    assert status == 200 and data["type"] == 4 and data["data"]["flags"] == 64
    assert "try again in" in data["data"]["content"]
    assert len(world.store.cases()) == 2
    _, other, _ = await ask(world, "refund annual plan", user=OTHER)
    assert other == {"type": 5}


async def test_spend_cap_stops_ai_and_hands_over():
    w = make_world(daily_spend_cap_usd=0.0)
    await ask(w, "refund annual plan")
    assert w.ai.calls == []
    assert w.store.case(1)["answer_status"] == "spend_cap"
    assert "spend limit" in msgs(w, threads(w)[0]["id"])[-2]["content"]
    assert w.store.case(1)["status"] == "handed_over"


async def test_spend_is_recorded_from_real_token_counts(world):
    await ask(world, "refund annual plan")
    today = world.store.spend_today()
    assert today["calls"] == 1 and today["tokens_in"] == 900 and today["reserved"] == 0
    assert today["spent"] > 0


async def test_ai_failure_is_reported_and_member_is_not_left_hanging():
    w = make_world(ai=FakeAI(answers=[RuntimeError("Claude timed out")]))
    await ask(w, "refund annual plan")
    assert "went wrong on my side" in msgs(w, threads(w)[0]["id"])[-2]["content"]
    mod_texts = [m["content"] for m in msgs(w, w.mod)]
    assert any("Support bot problem" in t and "Claude timed out" in t for t in mod_texts)
    assert w.store.case(1)["status"] == "handed_over"
    assert w.store.spend_today()["reserved"] == 0   # hold released


async def test_thread_failure_tells_the_member_and_alerts(world):
    world.fake.fail_next["thread"] = 403
    await ask(world, "refund annual plan")
    original = msgs(world, world.general)[0]
    assert "couldn't open a thread" in original["content"]
    assert any("Support bot problem" in m["content"] for m in msgs(world, world.mod))


async def test_duplicate_delivery_is_handled_once(world):
    p = world.side.slash("support", ASKER, world.general, question="refund annual plan")
    await world.side.send(p)
    _, again, _ = await world.side.send(p)
    await world.bot.drain()
    assert "already being handled" in again["data"]["content"]
    assert len(world.store.cases()) == 1 and len(threads(world)) == 1


async def test_initial_response_is_fast_even_when_claude_is_slow(world):
    import asyncio

    class SlowAI(FakeAI):
        async def answer(self, q, hits):
            await asyncio.sleep(0.5)
            return await super().answer(q, hits)
    w = make_world(ai=SlowAI())
    status, data, secs = await w.side.send(w.side.slash("support", ASKER, w.general, question="refund annual plan"))
    assert data == {"type": 5} and secs < 0.4
    await w.bot.drain()
    assert w.store.case(1)["answer_status"] == "answered"


async def test_dm_interactions_are_refused(world):
    p = world.side.slash("support", ASKER, world.general, question="x")
    p.pop("guild_id")
    p["user"] = p.pop("member")["user"]
    _, data, _ = await world.side.send(p)
    assert "only work inside the server" in data["data"]["content"]


async def test_empty_question(world):
    _, data, _ = await world.side.send(world.side.slash("support", ASKER, world.general, question="   "))
    assert "Please type your question" in data["data"]["content"]


async def test_health(world):
    import httpx
    async with httpx.AsyncClient(transport=world.side.transport, base_url="http://bot") as c:
        r = await c.get("/health")
    assert r.json()["ok"] is True and r.json()["cap_usd"] == 1.0
