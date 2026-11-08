import json

import pytest

from support_bot import cli
from support_bot.config import Settings
from support_bot.mock_discord import FakeDiscord

from .conftest import KB


@pytest.fixture
def env(monkeypatch):
    fake = FakeDiscord()
    for k, v in {"DISCORD_APPLICATION_ID": fake.application_id, "DISCORD_BOT_TOKEN": fake.bot_token,
                 "DISCORD_GUILD_ID": fake.guild_id, "DISCORD_API_BASE": "http://discord/api/v10", "KB_DIR": KB}.items():
        monkeypatch.setenv(k, v)
    for k in ("DISCORD_PUBLIC_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return fake


def args(*a):
    import argparse
    ns = argparse.Namespace(no_keychain=True, dry_run=False, global_=False, offline=False)
    for k, v in a:
        setattr(ns, k, v)
    return ns


def test_register_dry_run_prints_json(capsys):
    assert cli.main(["--no-keychain", "register", "--dry-run"]) == 0
    assert [c["name"] for c in json.loads(capsys.readouterr().out)][0] == "support"


def test_register_against_mock(env, capsys):
    assert cli.cmd_register(args(), transport=env.transport()) == 0
    assert "Registered 4 commands in guild" in capsys.readouterr().out
    assert len(env.commands[(env.application_id, env.guild_id)]) == 4


def test_invite_url(env, capsys):
    assert cli.main(["--no-keychain", "invite-url"]) == 0
    assert "applications.commands" in capsys.readouterr().out


def test_check_lists_what_is_missing_without_printing_secrets(env, capsys):
    assert cli.cmd_check(args(("offline", True))) == 1
    out = capsys.readouterr().out
    assert "public key missing" in out and "Anthropic key missing" in out
    assert env.bot_token not in out


def test_check_passes_when_complete(env, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "ab" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for k in ("SUPPORT_CHANNEL_ID", "MOD_CHANNEL_ID"):
        monkeypatch.setenv(k, "1")
    assert cli.cmd_check(args(), transport=env.transport()) == 0
    assert "bot token works" in capsys.readouterr().out


def test_kb_search(env, capsys):
    assert cli.main(["--no-keychain", "kb-search", "refund annual"]) == 0
    assert "billing.md > Refunds" in capsys.readouterr().out.splitlines()[0]


def test_ask_not_in_docs_makes_no_call(env, capsys):
    assert cli.main(["--no-keychain", "ask", "best bread recipe"]) == 0
    assert "no model call" in capsys.readouterr().out


def test_settings_repr_hides_secrets():
    s = Settings(bot_token="SECRET-TOKEN", anthropic_api_key="SECRET-KEY")
    assert "SECRET" not in repr(s)


def test_settings_from_env_flags(monkeypatch):
    s = Settings.from_env({"ENABLE_GATEWAY": "true", "DAILY_SPEND_CAP_USD": "2.5"}, use_keychain=False)
    assert s.enable_gateway and s.daily_spend_cap_usd == 2.5 and not s.message_content_intent
