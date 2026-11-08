"""Light moderation. The bot only ever FLAGS and REPORTS: members report a message with the
'Report to moderators' message command, and (with the optional gateway switched on) a few
cheap rules flag obvious spam. Removing a message always takes a moderator's button press."""
import re
import time
from collections import defaultdict, deque
from typing import Optional

INVITE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/[\w-]+", re.I)
DEFAULT_BLOCKED = ("free nitro", "claim your prize", "crypto giveaway", "steamcommunlty")


class Screener:
    """Rules for MESSAGE_CREATE events. The mention rule works without the privileged Message
    Content intent; the link, word and repeat rules need it, because Discord sends an empty
    content field to apps that don't have it."""

    def __init__(self, max_mentions: int = 5, repeat_count: int = 3, repeat_window_s: int = 60,
                 blocked=DEFAULT_BLOCKED, clock=time.time):
        self.max_mentions, self.repeat_count, self.repeat_window_s = max_mentions, repeat_count, repeat_window_s
        self.blocked = tuple(b.lower() for b in blocked)
        self.clock = clock
        self.recent = defaultdict(deque)

    def screen(self, msg: dict) -> Optional[str]:
        author = msg.get("author") or {}
        if author.get("bot") or msg.get("webhook_id") or not msg.get("guild_id"):
            return None
        mentions = len(msg.get("mentions") or []) + len(msg.get("mention_roles") or [])
        if msg.get("mention_everyone"):
            return "mentions @everyone or @here"
        if mentions >= self.max_mentions:
            return f"mentions {mentions} members or roles in one message"
        content = (msg.get("content") or "").strip()
        if not content:
            return None
        low = content.lower()
        if INVITE.search(content):
            return "posts a Discord invite link"
        for word in self.blocked:
            if word in low:
                return f"contains a blocked phrase ({word})"
        now = self.clock()
        q = self.recent[author.get("id")]
        q.append((now, low))
        while q and now - q[0][0] > self.repeat_window_s:
            q.popleft()
        if sum(1 for _, text in q if text == low) >= self.repeat_count:
            return f"repeated the same message {self.repeat_count} times in {self.repeat_window_s}s"
        return None


def jump_link(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
