"""The commands the bot registers, and the invite link. Registration is a bulk overwrite
(PUT), so running it twice changes nothing; guild commands update instantly, global ones
are for production."""
from urllib.parse import urlencode

CHAT_INPUT, USER, MESSAGE = 1, 2, 3
STRING = 3
MANAGE_MESSAGES = 1 << 13
MANAGE_THREADS = 1 << 34

COMMANDS = [
    {
        "name": "support", "type": CHAT_INPUT,
        "description": "Ask a question. The bot answers from the help docs in a new thread.",
        "options": [{"type": STRING, "name": "question", "description": "What do you need help with?",
                     "required": True, "max_length": 500}],
    },
    {
        "name": "human", "type": CHAT_INPUT,
        "description": "Hand this support thread over to a moderator.",
    },
    {
        "name": "close", "type": CHAT_INPUT,
        "description": "Close this support case (moderators).",
        "default_member_permissions": str(MANAGE_THREADS),
    },
    {"name": "Report to moderators", "type": MESSAGE, "description": ""},
]

# What the bot needs and nothing more: see docs/PLATFORM-FACTS.md for each bit.
PERMISSIONS = {
    "VIEW_CHANNEL": 1 << 10,
    "SEND_MESSAGES": 1 << 11,
    "MANAGE_MESSAGES": 1 << 13,          # only used after a moderator presses "Remove message"
    "READ_MESSAGE_HISTORY": 1 << 16,
    "MANAGE_THREADS": 1 << 34,           # archive a case thread when it is solved or closed
    "CREATE_PUBLIC_THREADS": 1 << 35,
    "SEND_MESSAGES_IN_THREADS": 1 << 38,
}


def permission_bits() -> int:
    total = 0
    for bit in PERMISSIONS.values():
        total |= bit
    return total


def invite_url(application_id: str, guild_id: str = "") -> str:
    params = {"client_id": application_id, "scope": "bot applications.commands", "permissions": permission_bits()}
    if guild_id:
        params.update(guild_id=guild_id, disable_guild_select="true")
    return "https://discord.com/oauth2/authorize?" + urlencode(params)


def validate(commands: list) -> list:
    """The documented limits, checked before anything is sent."""
    import re
    errors = []
    for c in commands:
        name, kind = c.get("name", ""), c.get("type", CHAT_INPUT)
        if not 1 <= len(name) <= 32:
            errors.append(f"{name!r}: name must be 1-32 characters")
        if kind == CHAT_INPUT:
            if not re.fullmatch(r"[-_\w]{1,32}", name) or name != name.lower():
                errors.append(f"{name!r}: slash command names are lowercase letters, numbers, - and _")
            if not 1 <= len(c.get("description", "")) <= 100:
                errors.append(f"{name!r}: description must be 1-100 characters")
        elif c.get("description", ""):
            errors.append(f"{name!r}: user and message commands must have an empty description")
        for o in c.get("options", []):
            if not 1 <= len(o.get("description", "")) <= 100:
                errors.append(f"{name!r}/{o.get('name')}: option description must be 1-100 characters")
        if len(c.get("options", [])) > 25:
            errors.append(f"{name!r}: at most 25 options")
    return errors
