"""Optional gateway connection (ENABLE_GATEWAY=true) for MESSAGE_CREATE events, used only
for the moderation rules. Interactions never need it. It follows the documented handshake:
Hello (op 10) -> Identify (op 2) with intents -> Ready, heartbeats (op 1) on the interval
with jitter, reconnects on a dropped connection, Reconnect (op 7) or Invalid Session (op 9),
and stops with a clear error on fatal close codes (bad token, disallowed intents...).
Session resume is not implemented: a reconnect identifies again."""
import asyncio
import json
import logging
import random

log = logging.getLogger("support_bot.gateway")

GUILDS = 1 << 0
GUILD_MESSAGES = 1 << 9
MESSAGE_CONTENT = 1 << 15  # privileged: switch on in the Developer Portal first
FATAL = {4004: "authentication failed (check the bot token)", 4010: "invalid shard", 4011: "sharding required",
         4012: "invalid API version", 4013: "invalid intents", 4014: "disallowed intents (switch the privileged "
         "intent on in the Developer Portal, or turn MESSAGE_CONTENT_INTENT off)"}


class GatewayFatal(Exception):
    pass


def intents(message_content: bool) -> int:
    return GUILDS | GUILD_MESSAGES | (MESSAGE_CONTENT if message_content else 0)


class Gateway:
    def __init__(self, token: str, url: str, on_message, message_content: bool = False, connect=None,
                 max_reconnects: int = 5):
        if connect is None:
            import websockets
            connect = websockets.connect
        self._token, self.url, self.on_message = token, url, on_message
        self.intents = intents(message_content)
        self.connect, self.max_reconnects = connect, max_reconnects
        self.seq = None
        self.ready = asyncio.Event()
        self.stopped = False

    async def run(self) -> None:
        failures = 0
        while not self.stopped:
            try:
                await self._session()
                failures = 0
            except GatewayFatal:
                raise
            except Exception as exc:
                failures += 1
                log.warning("gateway connection lost (%s), reconnect %d", exc, failures)
                if failures > self.max_reconnects:
                    raise
                await asyncio.sleep(min(30, 2 ** failures) * random.uniform(0.5, 1))

    async def _session(self) -> None:
        async with self.connect(self.url) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("op") != 10:
                raise RuntimeError(f"expected Hello, got op {hello.get('op')}")
            interval = hello["d"]["heartbeat_interval"] / 1000
            beat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token": self._token, "intents": self.intents,
                    "properties": {"os": "linux", "browser": "discord-claude-support-bot",
                                   "device": "discord-claude-support-bot"}}}))
                while not self.stopped:
                    try:
                        raw = await ws.recv()
                    except Exception as exc:
                        code = getattr(getattr(exc, "rcvd", None), "code", None) or getattr(ws, "close_code", None)
                        if code in FATAL:
                            raise GatewayFatal(f"gateway closed {code}: {FATAL[code]}") from exc
                        raise
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if msg.get("s") is not None:
                        self.seq = msg["s"]
                    if op == 0:
                        if msg.get("t") == "READY":
                            self.ready.set()
                        elif msg.get("t") == "MESSAGE_CREATE":
                            await self.on_message(msg["d"])
                    elif op == 1:
                        await ws.send(json.dumps({"op": 1, "d": self.seq}))
                    elif op in (7, 9):
                        log.info("gateway asked us to reconnect (op %s)", op)
                        return
            finally:
                beat.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    def stop(self):
        self.stopped = True
