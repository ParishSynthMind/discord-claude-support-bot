import pytest

from support_bot.discord_api import DiscordAPI, DiscordError
from support_bot.mock_discord import FakeDiscord


async def _no_sleep(_s):
    return None


@pytest.fixture
def fake():
    return FakeDiscord()


def api_for(fake, token=None):
    a = DiscordAPI(token if token is not None else fake.bot_token, "http://discord/api/v10", transport=fake.transport())
    a.sleep = _no_sleep
    return a


async def test_send_uses_bot_auth_user_agent_and_no_pings(fake):
    ch = fake.add_channel("general")
    await api_for(fake).send(ch, "hello")
    method, path, body, headers = fake.calls[-1]
    assert headers["authorization"] == f"Bot {fake.bot_token}"
    assert headers["user-agent"].startswith("DiscordBot (")
    assert body["allowed_mentions"] == {"parse": []}


async def test_wrong_token_is_401(fake):
    ch = fake.add_channel("general")
    with pytest.raises(DiscordError) as e:
        await api_for(fake, token="wrong").send(ch, "x")
    assert e.value.status == 401


async def test_unknown_channel_is_10003(fake):
    with pytest.raises(DiscordError) as e:
        await api_for(fake).send("999", "x")
    assert e.value.code == 10003


async def test_too_long_message_is_50035(fake):
    ch = fake.add_channel("general")
    with pytest.raises(DiscordError) as e:
        await api_for(fake).send(ch, "x" * 2001)
    assert e.value.code == 50035


async def test_thread_is_public_because_we_ask(fake):
    ch = fake.add_channel("support")
    t = await api_for(fake).start_thread(ch, "Case 1: " + "q" * 200)
    assert t["type"] == 11 and len(t["name"]) == 100 and t["parent_id"] == ch


async def test_mock_thread_default_is_private_as_documented(fake):
    import httpx
    ch = fake.add_channel("support")
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://discord") as c:
        r = await c.post(f"/api/v10/channels/{ch}/threads", json={"name": "x"},
                         headers={"Authorization": f"Bot {fake.bot_token}"})
    assert r.json()["type"] == 12


async def test_429_waits_retry_after_then_succeeds(fake):
    ch = fake.add_channel("general")
    fake.rate_limit_next["send"] = 0.25
    waits = []
    a = api_for(fake)

    async def record(s):
        waits.append(s)
    a.sleep = record
    msg = await a.send(ch, "after the limit")
    assert msg["content"] == "after the limit" and 0.25 in waits


async def test_429_longer_than_max_wait_raises(fake):
    ch = fake.add_channel("general")
    fake.rate_limit_next["send"] = 120
    with pytest.raises(DiscordError) as e:
        await api_for(fake).send(ch, "x")
    assert e.value.status == 429


async def test_5xx_is_retried(fake):
    ch = fake.add_channel("general")
    fake.fail_next["send"] = 502
    assert (await api_for(fake).send(ch, "ok"))["content"] == "ok"


async def test_delete_sends_audit_reason_and_unknown_is_10008(fake):
    ch = fake.add_channel("general")
    m = fake.add_message(ch, "1", "spam")
    a = api_for(fake)
    await a.delete_message(ch, m["id"], "Removed by moderator 9 after report 1")
    assert fake.calls[-1][3]["x-audit-log-reason"].startswith("Removed by moderator")
    with pytest.raises(DiscordError) as e:
        await a.delete_message(ch, m["id"], "again")
    assert e.value.code == 10008


async def test_expired_interaction_token_is_unknown_webhook(fake):
    ch = fake.add_channel("general")
    token = fake.issue_token(ch)
    fake.tokens[token]["responded"] = True
    fake.tokens[token]["issued"] -= 16 * 60
    with pytest.raises(DiscordError) as e:
        await api_for(fake).edit_original(fake.application_id, token, "late")
    assert e.value.code == 10015


async def test_followups_need_no_bot_token(fake):
    ch = fake.add_channel("general")
    token = fake.issue_token(ch)
    fake.tokens[token]["responded"] = True
    await api_for(fake, token="").edit_original(fake.application_id, token, "done")
    assert "authorization" not in fake.calls[-1][3]


async def test_allowed_mentions_parse_is_exclusive_in_mock(fake):
    ch = fake.add_channel("general")
    with pytest.raises(DiscordError):
        await api_for(fake).send(ch, "x", allowed_mentions={"parse": ["users"], "users": ["1"]})
