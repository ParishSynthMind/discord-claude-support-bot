from urllib.parse import parse_qs, urlparse

from support_bot.commands import COMMANDS, PERMISSIONS, invite_url, permission_bits, validate
from support_bot.discord_api import DiscordAPI
from support_bot.mock_discord import FakeDiscord


def test_shipped_commands_pass_discord_limits():
    assert validate(COMMANDS) == []


def test_validation_catches_bad_commands():
    bad = [{"name": "Support", "type": 1, "description": "x"}, {"name": "x" * 33, "type": 1, "description": "x"},
           {"name": "ok", "type": 1, "description": ""}, {"name": "Flag", "type": 3, "description": "no"}]
    assert len(validate(bad)) >= 4


def test_message_command_present_for_reports():
    report = [c for c in COMMANDS if c["type"] == 3]
    assert report and report[0]["name"] == "Report to moderators" and report[0]["description"] == ""


def test_invite_url_has_scopes_and_minimal_permissions():
    q = parse_qs(urlparse(invite_url("123", "456")).query)
    assert q["scope"] == ["bot applications.commands"]
    perms = int(q["permissions"][0])
    assert perms == permission_bits()
    assert not perms & (1 << 3)          # never Administrator
    assert not perms & (1 << 1) and not perms & (1 << 2)   # no kick, no ban
    assert q["guild_id"] == ["456"]


def test_permission_list_is_small():
    assert len(PERMISSIONS) == 7


async def test_registration_is_a_bulk_overwrite_and_idempotent():
    fake = FakeDiscord()
    api = DiscordAPI(fake.bot_token, "http://discord/api/v10", transport=fake.transport())
    first = await api.overwrite_commands(fake.application_id, COMMANDS, fake.guild_id)
    await api.overwrite_commands(fake.application_id, COMMANDS, fake.guild_id)
    saved = fake.commands[(fake.application_id, fake.guild_id)]
    assert len(saved) == len(first) == len(COMMANDS)
    assert fake.calls[-1][0] == "PUT"


async def test_registration_rejected_by_mock_for_wrong_app():
    import pytest

    from support_bot.discord_api import DiscordError
    fake = FakeDiscord()
    api = DiscordAPI(fake.bot_token, "http://discord/api/v10", transport=fake.transport())
    with pytest.raises(DiscordError) as e:
        await api.overwrite_commands("42", COMMANDS, fake.guild_id)
    assert e.value.code == 50001
