"""A faithful stand-in for the parts of Discord this bot touches, built from Discord's own docs
(see docs/PLATFORM-FACTS.md). Three pieces:

- FakeDiscord: the REST API v10 routes the bot calls, with Discord's validation and error
  codes (401 without a bot token, 10003 Unknown Channel, 10008 Unknown Message, 10015 Unknown
  Webhook for expired interaction tokens, 50035 Invalid Form Body for over-long content or bad
  commands, 429 with retry_after and X-RateLimit-* headers), threads that default to PRIVATE
  when no type is given (as documented), and a log of every call.
- DiscordSide: plays Discord sending interactions: builds payloads in the documented shape,
  signs them with an Ed25519 key (timestamp + raw body), POSTs them to the bot, enforces the
  3 second deadline for the initial response, and runs the endpoint check Discord does
  (a PING, plus a deliberately bad signature that must be refused).
- FakeGateway: Hello / Identify / Ready / heartbeat ACK over a real websocket, closing with
  4004 on a bad token and 4014 when a privileged intent wasn't switched on.
"""
import asyncio
import itertools
import json
import time
from typing import Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .signature import public_hex, sign

_ids = itertools.count(1_300_000_000_000_000_000)


def snowflake() -> str:
    return str(next(_ids))


def error(status: int, code: int, message: str, errors: Optional[dict] = None):
    body = {"code": code, "message": message}
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status)


class FakeDiscord:
    TOKEN_LIFETIME_S = 15 * 60

    def __init__(self, bot_token: str = "test-bot-token", application_id: str = "1100000000000000001",
                 guild_id: str = "1200000000000000001", clock=time.time):
        self.bot_token, self.application_id, self.guild_id, self.clock = bot_token, application_id, guild_id, clock
        self.channels = {}     # id -> {id, type, name, parent_id, archived, locked}
        self.messages = {}     # channel id -> {message id -> message}
        self.tokens = {}       # interaction token -> {issued, responded, channel_id, original_id}
        self.commands = {}     # (app, guild or "global") -> list
        self.calls = []        # (method, path, json)
        self.rate_limit_next = {}   # route key -> retry_after seconds for the next call
        self.fail_next = {}         # route key -> status code
        self.app = self._build()

    # setup helpers ---------------------------------------------------------
    def add_channel(self, name: str, kind: int = 0, channel_id: Optional[str] = None) -> str:
        cid = channel_id or snowflake()
        self.channels[cid] = {"id": cid, "type": kind, "name": name, "guild_id": self.guild_id}
        self.messages[cid] = {}
        return cid

    def add_message(self, channel_id: str, author_id: str, content: str, **extra) -> dict:
        mid = snowflake()
        msg = {"id": mid, "channel_id": channel_id, "author": {"id": author_id, "username": f"user{author_id[-3:]}"},
               "content": content, "mentions": [], "mention_roles": [], "mention_everyone": False, **extra}
        self.messages[channel_id][mid] = msg
        return msg

    def issue_token(self, channel_id: str) -> str:
        token = "aW50ZXJhY3Rpb246" + snowflake()
        self.tokens[token] = {"issued": self.clock(), "responded": False, "channel_id": channel_id, "original_id": None}
        return token

    def thread_messages(self, thread_id: str) -> list:
        return list(self.messages.get(thread_id, {}).values())

    # the app ---------------------------------------------------------------
    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        def auth_ok(request: Request) -> bool:
            return request.headers.get("Authorization") == f"Bot {fake.bot_token}"

        def gate(request: Request, key: str):
            if key in fake.rate_limit_next:
                retry = fake.rate_limit_next.pop(key)
                return JSONResponse({"message": "You are being rate limited.", "retry_after": retry, "global": False},
                                    status_code=429, headers={"Retry-After": str(int(retry) + 1),
                                                              "X-RateLimit-Scope": "user"})
            if key in fake.fail_next:
                return error(fake.fail_next.pop(key), 0, "fake failure")
            return None

        def limits(resp: Response, remaining: int = 4):
            resp.headers.update({"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": str(remaining),
                                 "X-RateLimit-Reset-After": "0.01", "X-RateLimit-Bucket": "fakebucket"})
            return resp

        async def body_of(request: Request):
            raw = await request.body()
            data = json.loads(raw) if raw else None
            fake.calls.append((request.method, request.url.path, data, dict(request.headers)))
            return data

        def check_message(data: dict):
            content = data.get("content") or ""
            if len(content) > 2000:
                return error(400, 50035, "Invalid Form Body",
                             {"content": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH",
                                                       "message": "Must be 2000 or fewer in length."}]}})
            am = data.get("allowed_mentions") or {}
            if "parse" in am and (("users" in am and "users" in am["parse"]) or ("roles" in am and "roles" in am["parse"])):
                return error(400, 50035, "Invalid Form Body (allowed_mentions parse is exclusive with users/roles)")
            return None

        def new_message(channel_id: str, data: dict, author_id: str) -> dict:
            mid = snowflake()
            msg = {"id": mid, "channel_id": channel_id, "author": {"id": author_id, "bot": True},
                   "content": data.get("content", ""), "components": data.get("components", []),
                   "allowed_mentions": data.get("allowed_mentions"), "flags": data.get("flags", 0),
                   "timestamp": fake.clock()}
            fake.messages.setdefault(channel_id, {})[mid] = msg
            return msg

        @app.post("/api/v10/channels/{channel_id}/messages")
        async def create_message(channel_id: str, request: Request):
            data = await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            if (r := gate(request, "send")) is not None:
                return r
            ch = fake.channels.get(channel_id)
            if not ch:
                return error(404, 10003, "Unknown Channel")
            if ch.get("archived") and ch.get("locked"):
                return error(400, 50083, "Thread is archived")
            if (bad := check_message(data or {})) is not None:
                return bad
            ch["archived"] = False  # posting in an unlocked archived thread un-archives it
            return limits(JSONResponse(new_message(channel_id, data, fake.application_id)))

        @app.patch("/api/v10/channels/{channel_id}/messages/{message_id}")
        async def edit_message(channel_id: str, message_id: str, request: Request):
            data = await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            msg = fake.messages.get(channel_id, {}).get(message_id)
            if not msg:
                return error(404, 10008, "Unknown Message")
            msg.update({k: v for k, v in data.items() if k in ("content", "components")})
            return JSONResponse(msg)

        @app.delete("/api/v10/channels/{channel_id}/messages/{message_id}")
        async def delete_message(channel_id: str, message_id: str, request: Request):
            await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            if (r := gate(request, "delete")) is not None:
                return r
            if fake.messages.get(channel_id, {}).pop(message_id, None) is None:
                return error(404, 10008, "Unknown Message")
            return Response(status_code=204)

        @app.post("/api/v10/channels/{channel_id}/threads")
        async def start_thread(channel_id: str, request: Request):
            data = await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            if (r := gate(request, "thread")) is not None:
                return r
            parent = fake.channels.get(channel_id)
            if not parent:
                return error(404, 10003, "Unknown Channel")
            name = (data or {}).get("name", "")
            if not 1 <= len(name) <= 100:
                return error(400, 50035, "Invalid Form Body", {"name": {"_errors": [{"code": "BASE_TYPE_BAD_LENGTH"}]}})
            if (data or {}).get("auto_archive_duration", 1440) not in (60, 1440, 4320, 10080):
                return error(400, 50035, "Invalid Form Body (auto_archive_duration)")
            tid = fake.add_channel(name, kind=(data or {}).get("type", 12))  # documented default: PRIVATE_THREAD
            fake.channels[tid].update(parent_id=channel_id, archived=False, locked=False)
            return limits(JSONResponse(fake.channels[tid], status_code=201))

        @app.patch("/api/v10/channels/{channel_id}")
        async def modify_channel(channel_id: str, request: Request):
            data = await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            ch = fake.channels.get(channel_id)
            if not ch:
                return error(404, 10003, "Unknown Channel")
            ch.update({k: v for k, v in (data or {}).items() if k in ("archived", "locked", "name")})
            return JSONResponse(ch)

        def live_token(token: str):
            t = fake.tokens.get(token)
            if not t or fake.clock() - t["issued"] > fake.TOKEN_LIFETIME_S:
                return None
            return t

        @app.patch("/api/v10/webhooks/{app_id}/{token}/messages/@original")
        async def edit_original(app_id: str, token: str, request: Request):
            data = await body_of(request)
            t = live_token(token)
            if app_id != fake.application_id or not t:
                return error(404, 10015, "Unknown Webhook")
            # In real life the bot's HTTP response reaches Discord before any follow-up call can;
            # in-process the two can race, so give the response a moment to be recorded first.
            for _ in range(100):
                if t["responded"]:
                    break
                await asyncio.sleep(0.01)
            if not t["responded"]:
                return error(404, 10008, "Unknown Message")  # nothing to edit before an initial response
            if (bad := check_message(data or {})) is not None:
                return bad
            if t["original_id"] is None:
                t["original_id"] = new_message(t["channel_id"], data, fake.application_id)["id"]
            msg = fake.messages[t["channel_id"]][t["original_id"]]
            msg.update(content=data.get("content", msg["content"]), components=data.get("components", msg.get("components")))
            return JSONResponse(msg)

        @app.post("/api/v10/webhooks/{app_id}/{token}")
        async def followup(app_id: str, token: str, request: Request):
            data = await body_of(request)
            t = live_token(token)
            if app_id != fake.application_id or not t:
                return error(404, 10015, "Unknown Webhook")
            if (bad := check_message(data or {})) is not None:
                return bad
            return JSONResponse(new_message(t["channel_id"], data, fake.application_id))

        @app.put("/api/v10/applications/{app_id}/commands")
        @app.put("/api/v10/applications/{app_id}/guilds/{guild_id}/commands")
        async def overwrite(app_id: str, request: Request, guild_id: Optional[str] = None):
            data = await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            if app_id != fake.application_id:
                return error(403, 50001, "Missing Access")
            from .commands import validate
            problems = validate(data or [])
            if problems:
                return error(400, 50035, "Invalid Form Body", {"commands": problems})
            saved = [{**c, "id": snowflake(), "application_id": app_id, "version": snowflake()} for c in data]
            fake.commands[(app_id, guild_id or "global")] = saved
            return JSONResponse(saved)

        @app.get("/api/v10/users/@me")
        async def me(request: Request):
            await body_of(request)
            if not auth_ok(request):
                return error(401, 0, "401: Unauthorized")
            return JSONResponse({"id": fake.application_id, "username": "Lumen Helper", "bot": True})

        return app

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)


class DiscordSide:
    """Sends interactions to the bot the way Discord does."""

    DEADLINE_S = 3.0

    def __init__(self, fake: FakeDiscord, bot_url: str = "http://bot/interactions",
                 transport: Optional[httpx.AsyncBaseTransport] = None, clock=time.time):
        self.fake, self.bot_url, self.transport, self.clock = fake, bot_url, transport, clock
        self.key = Ed25519PrivateKey.generate()
        self.public_key = public_hex(self.key)
        self.sent = []  # (payload, status, response json, seconds)

    def payload(self, kind: int, data: Optional[dict], user_id: str, channel_id: str, permissions: int = 0,
                message: Optional[dict] = None) -> dict:
        p = {"id": snowflake(), "application_id": self.fake.application_id, "type": kind,
             "guild_id": self.fake.guild_id, "channel_id": channel_id,
             "channel": {"id": channel_id, "type": self.fake.channels.get(channel_id, {}).get("type", 0)},
             "member": {"user": {"id": user_id, "username": f"user{user_id[-3:]}", "global_name": None},
                        "roles": [], "permissions": str(permissions)},
             "token": self.fake.issue_token(channel_id), "version": 1, "locale": "en-GB",
             "app_permissions": "2147483647", "entitlements": [], "authorizing_integration_owners": {"0": self.fake.guild_id},
             "context": 0}
        if data is not None:
            p["data"] = data
        if message is not None:
            p["message"] = message
        return p

    def slash(self, name: str, user_id: str, channel_id: str, permissions: int = 0, **options) -> dict:
        data = {"id": snowflake(), "name": name, "type": 1,
                "options": [{"name": k, "type": 3, "value": v} for k, v in options.items()]}
        if not options:
            del data["options"]
        return self.payload(2, data, user_id, channel_id, permissions)

    def message_command(self, name: str, user_id: str, target: dict, permissions: int = 0) -> dict:
        data = {"id": snowflake(), "name": name, "type": 3, "target_id": target["id"],
                "resolved": {"messages": {target["id"]: target}}}
        return self.payload(2, data, user_id, target["channel_id"], permissions)

    def button(self, custom_id: str, user_id: str, message: dict, permissions: int = 0) -> dict:
        return self.payload(3, {"custom_id": custom_id, "component_type": 2}, user_id, message["channel_id"],
                            permissions, message=message)

    async def send(self, payload: dict, signature: Optional[str] = None, timestamp: Optional[str] = None,
                   raw: Optional[bytes] = None):
        body = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
        ts = timestamp or str(int(self.clock()))
        sig = signature if signature is not None else sign(self.key, ts, body)
        started = time.perf_counter()
        async with httpx.AsyncClient(transport=self.transport, timeout=self.DEADLINE_S) as c:
            resp = await c.post(self.bot_url, content=body, headers={
                "Content-Type": "application/json", "User-Agent": "Discord-Interactions/1.0 (+https://discord.com)",
                "X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts})
        seconds = time.perf_counter() - started
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None
        if resp.status_code == 200 and seconds <= self.DEADLINE_S and payload.get("token") in self.fake.tokens:
            t = self.fake.tokens[payload["token"]]
            t["responded"] = True
            if data and data.get("type") == 4:  # the response itself becomes the original message
                t["original_id"] = self._post_response(payload, data)
            if data and data.get("type") == 7 and payload.get("message"):  # UPDATE_MESSAGE edits the clicked message
                target = self.fake.messages.get(payload["message"]["channel_id"], {}).get(payload["message"]["id"])
                if target is not None:
                    d = data.get("data") or {}
                    target.update({k: d[k] for k in ("content", "components") if k in d})
        self.sent.append((payload, resp.status_code, data, seconds))
        return resp.status_code, data, seconds

    def _post_response(self, payload: dict, data: dict) -> str:
        mid = snowflake()
        d = data.get("data") or {}
        self.fake.messages.setdefault(payload["channel_id"], {})[mid] = {
            "id": mid, "channel_id": payload["channel_id"], "author": {"id": self.fake.application_id, "bot": True},
            "content": d.get("content", ""), "components": d.get("components", []), "flags": d.get("flags", 0),
            "interaction_user": ((payload.get("member") or {}).get("user") or payload.get("user") or {}).get("id")}
        return mid

    async def endpoint_check(self) -> dict:
        """What Discord does when the endpoint URL is saved, plus its routine bad-signature probe."""
        ping = {"id": snowflake(), "application_id": self.fake.application_id, "type": 1, "version": 1, "token": "x"}
        good_status, good, _ = await self.send(ping)
        bad_status, _, _ = await self.send(ping, signature="00" * 64)
        missing_status, _, _ = await self.send(ping, signature="")
        return {"ping_ok": good_status == 200 and good == {"type": 1},
                "bad_signature_refused": bad_status == 401, "missing_signature_refused": missing_status == 401}


class FakeGateway:
    """A websocket server speaking the gateway handshake, for the optional MESSAGE_CREATE feed."""

    def __init__(self, bot_token: str = "test-bot-token", allow_message_content: bool = False,
                 heartbeat_ms: int = 200):
        self.bot_token, self.allow_message_content, self.heartbeat_ms = bot_token, allow_message_content, heartbeat_ms
        self.identified = []
        self.heartbeats = 0
        self.clients = set()
        self.seq = 0
        self.server = None
        self.url = None

    async def start(self):
        import websockets
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = list(self.server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/?v=10&encoding=json"
        return self.url

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws):
        import websockets
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms}, "s": None, "t": None}))
        try:
            await self._loop(ws)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def _loop(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["op"] == 2:
                d = msg["d"]
                self.identified.append(d)
                if d.get("token") != self.bot_token:
                    await ws.close(4004, "Authentication failed.")
                    return
                if d.get("intents", 0) & (1 << 15) and not self.allow_message_content:
                    await ws.close(4014, "Disallowed intent(s).")
                    return
                self.seq += 1
                await ws.send(json.dumps({"op": 0, "t": "READY", "s": self.seq,
                                          "d": {"v": 10, "session_id": "fake", "user": {"id": "1", "bot": True}}}))
                self.clients.add(ws)
            elif msg["op"] == 1:
                self.heartbeats += 1
                await ws.send(json.dumps({"op": 11, "d": None, "s": None, "t": None}))

    async def dispatch_message(self, message: dict):
        """Send MESSAGE_CREATE to every identified client. Without the Message Content intent,
        Discord blanks the content (except for DMs and messages that mention the bot)."""
        for ws in list(self.clients):
            self.seq += 1
            ident = self.identified[-1]
            m = dict(message)
            if not ident.get("intents", 0) & (1 << 15):
                m["content"] = ""
            await ws.send(json.dumps({"op": 0, "t": "MESSAGE_CREATE", "s": self.seq, "d": m}))
        await asyncio.sleep(0.05)
