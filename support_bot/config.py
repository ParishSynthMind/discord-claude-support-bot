"""Settings. Ids and the public key are not secret; the bot token and the Anthropic key are,
and come from the environment or, on macOS, the Keychain (never from a file in the repo)."""
import os
import subprocess
from dataclasses import dataclass, field
from typing import Mapping, Optional

KEYCHAIN_ITEMS = {
    "DISCORD_BOT_TOKEN": "discord-support-bot-token",
    "DISCORD_PUBLIC_KEY": "discord-support-public-key",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
}


def keychain(item: str, account: str = "neo") -> Optional[str]:
    """Read one generic password from the macOS Keychain (this account first, then any
    account). Returns None anywhere else."""
    for who in (["-a", account], []):
        try:
            out = subprocess.run(["security", "find-generic-password", *who, "-s", item, "-w"],
                                 capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        value = out.stdout.strip()
        if out.returncode == 0 and value:
            return value
    return None


def secret(name: str, env: Mapping[str, str] = os.environ, use_keychain: bool = True) -> Optional[str]:
    if env.get(name):
        return env[name]
    if use_keychain and name in KEYCHAIN_ITEMS:
        return keychain(KEYCHAIN_ITEMS[name])
    return None


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    application_id: str = ""
    public_key: str = ""                 # hex, from the Developer Portal General Information page
    bot_token: Optional[str] = field(default=None, repr=False)
    anthropic_api_key: Optional[str] = field(default=None, repr=False)
    guild_id: str = ""
    support_channel_id: str = ""         # where case threads are opened
    mod_channel_id: str = ""             # private channel for handovers and reports
    mod_role_id: str = ""                # pinged on a handover, nothing else is ever pinged
    community: str = "Lumen Notes"
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    enable_gateway: bool = False
    message_content_intent: bool = False
    model: str = "claude-sonnet-5-5"
    price_in_per_m: float = 2.00         # USD per million input tokens (claude-sonnet-5-5)
    price_out_per_m: float = 10.00
    max_output_tokens: int = 700
    daily_spend_cap_usd: float = 1.00
    support_per_user: int = 5            # /support questions per user per window
    support_window_s: int = 3600
    reports_per_user: int = 10           # message reports per user per window
    reports_window_s: int = 3600
    signature_max_age_s: int = 300       # 0 switches the timestamp age check off
    kb_dir: str = "knowledge_base"
    db_path: str = "support_bot.db"
    alert_webhook_url: str = ""
    triage_reports: bool = True          # ask Claude to label reported messages (within the spend cap)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, use_keychain: bool = True) -> "Settings":
        e = lambda k, d="": env.get(k, d)
        return cls(
            application_id=e("DISCORD_APPLICATION_ID"),
            public_key=secret("DISCORD_PUBLIC_KEY", env, use_keychain) or "",
            bot_token=secret("DISCORD_BOT_TOKEN", env, use_keychain),
            anthropic_api_key=secret("ANTHROPIC_API_KEY", env, use_keychain),
            guild_id=e("DISCORD_GUILD_ID"),
            support_channel_id=e("SUPPORT_CHANNEL_ID"),
            mod_channel_id=e("MOD_CHANNEL_ID"),
            mod_role_id=e("MOD_ROLE_ID"),
            community=e("COMMUNITY_NAME", "Lumen Notes"),
            api_base=e("DISCORD_API_BASE", "https://discord.com/api/v10"),
            gateway_url=e("DISCORD_GATEWAY_URL", "wss://gateway.discord.gg/?v=10&encoding=json"),
            enable_gateway=_flag(e("ENABLE_GATEWAY", "false")),
            message_content_intent=_flag(e("MESSAGE_CONTENT_INTENT", "false")),
            model=e("CLAUDE_MODEL", "claude-sonnet-5-5"),
            daily_spend_cap_usd=float(e("DAILY_SPEND_CAP_USD", "1.00")),
            support_per_user=int(e("SUPPORT_PER_USER", "5")),
            support_window_s=int(e("SUPPORT_WINDOW_S", "3600")),
            reports_per_user=int(e("REPORTS_PER_USER", "10")),
            reports_window_s=int(e("REPORTS_WINDOW_S", "3600")),
            signature_max_age_s=int(e("SIGNATURE_MAX_AGE_S", "300")),
            kb_dir=e("KB_DIR", "knowledge_base"),
            db_path=e("DB_PATH", "support_bot.db"),
            alert_webhook_url=e("ALERT_WEBHOOK_URL"),
            triage_reports=_flag(e("TRIAGE_REPORTS", "true")),
        )

    def problems(self) -> list:
        """Everything missing for a real run, in plain words (never prints a secret)."""
        out = []
        if not self.application_id:
            out.append("DISCORD_APPLICATION_ID is not set")
        if len(self.public_key) != 64:
            out.append("public key missing or not 64 hex characters (Keychain item discord-support-public-key)")
        if not self.bot_token:
            out.append("bot token missing (Keychain item discord-support-bot-token)")
        if not self.anthropic_api_key:
            out.append("Anthropic key missing (Keychain item anthropic-api-key)")
        for name in ("guild_id", "support_channel_id", "mod_channel_id"):
            if not getattr(self, name):
                out.append(f"{name.upper()} is not set")
        return out
