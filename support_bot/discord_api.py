"""The slice of the Discord REST API (v10) the bot uses, with the documented rate-limit
handling: per-bucket waits from X-RateLimit-Remaining / Reset-After, and on a 429 a wait of
retry_after from the JSON body before trying again. Every message is sent with
allowed_mentions so nothing pings anyone unless the code says so."""
import asyncio
import logging
from typing import Optional

import httpx

from . import __version__

log = logging.getLogger("support_bot.discord")

NO_PINGS = {"parse": []}
PUBLIC_THREAD = 11
EPHEMERAL = 1 << 6


class DiscordError(Exception):
    def __init__(self, status: int, code: int, message: str, route: str):
        super().__init__(f"Discord {status} (code {code}) on {route}: {message}")
        self.status, self.code, self.message, self.route = status, code, message, route


class DiscordAPI:
    def __init__(self, token: Optional[str], base: str = "https://discord.com/api/v10",
                 transport: Optional[httpx.AsyncBaseTransport] = None, max_retries: int = 3,
                 max_wait_s: float = 30.0, sleep=asyncio.sleep):
        headers = {"User-Agent": f"DiscordBot (https://github.com/ParishSynthMind/discord-claude-support-bot, {__version__})"}
        self._token = token
        self.client = httpx.AsyncClient(base_url=base, headers=headers, transport=transport, timeout=15)
        self.max_retries, self.max_wait_s, self.sleep = max_retries, max_wait_s, sleep
        self.bucket_wait: dict = {}

    async def aclose(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, *, json=None, auth: bool = True, reason: Optional[str] = None,
                      route: Optional[str] = None):
        route = route or f"{method} {path}"
        headers = {}
        if auth:
            if not self._token:
                raise DiscordError(401, 0, "no bot token configured", route)
            headers["Authorization"] = f"Bot {self._token}"
        if reason:
            headers["X-Audit-Log-Reason"] = reason
        for attempt in range(self.max_retries + 1):
            wait = self.bucket_wait.pop(route, 0)
            if wait > 0:
                await self.sleep(min(wait, self.max_wait_s))
            resp = await self.client.request(method, path, json=json, headers=headers)
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    self.bucket_wait[route] = float(resp.headers.get("X-RateLimit-Reset-After", "0"))
                except ValueError:
                    pass
            if resp.status_code == 429:
                body = _json(resp)
                retry = float(body.get("retry_after", resp.headers.get("Retry-After", 1)) or 1)
                log.warning("429 on %s (global=%s), waiting %.2fs", route, body.get("global"), retry)
                if attempt == self.max_retries or retry > self.max_wait_s:
                    raise DiscordError(429, body.get("code", 0), body.get("message", "rate limited"), route)
                await self.sleep(retry)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                await self.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code >= 400:
                body = _json(resp)
                raise DiscordError(resp.status_code, body.get("code", 0), body.get("message", resp.text[:200]), route)
            return _json(resp) if resp.content else None
        raise DiscordError(599, 0, "gave up after retries", route)  # pragma: no cover

    # messages and threads ----------------------------------------------------
    async def send(self, channel_id: str, content: str, components=None, allowed_mentions=None):
        body = {"content": content, "allowed_mentions": allowed_mentions or NO_PINGS}
        if components:
            body["components"] = components
        return await self.request("POST", f"/channels/{channel_id}/messages", json=body,
                                  route="POST /channels/{id}/messages")

    async def edit_message(self, channel_id: str, message_id: str, content: str, components=None):
        body = {"content": content, "allowed_mentions": NO_PINGS, "components": components or []}
        return await self.request("PATCH", f"/channels/{channel_id}/messages/{message_id}", json=body,
                                  route="PATCH /channels/{id}/messages/{id}")

    async def delete_message(self, channel_id: str, message_id: str, reason: str):
        return await self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}", reason=reason,
                                  route="DELETE /channels/{id}/messages/{id}")

    async def start_thread(self, channel_id: str, name: str, auto_archive_minutes: int = 1440):
        # Discord's default thread type is PRIVATE_THREAD, so ask for a public one explicitly.
        body = {"name": name[:100], "type": PUBLIC_THREAD, "auto_archive_duration": auto_archive_minutes}
        return await self.request("POST", f"/channels/{channel_id}/threads", json=body,
                                  route="POST /channels/{id}/threads")

    async def archive_thread(self, thread_id: str, reason: str, lock: bool = False):
        return await self.request("PATCH", f"/channels/{thread_id}", json={"archived": True, "locked": lock},
                                  reason=reason, route="PATCH /channels/{id}")

    # interaction follow-ups (webhook routes: the interaction token is the credential) --------------
    async def edit_original(self, application_id: str, token: str, content: str, components=None, allowed_mentions=None):
        body = {"content": content, "allowed_mentions": allowed_mentions or NO_PINGS}
        if components is not None:
            body["components"] = components
        return await self.request("PATCH", f"/webhooks/{application_id}/{token}/messages/@original", json=body,
                                  auth=False, route="PATCH /webhooks/{app}/{token}/messages/@original")

    async def followup(self, application_id: str, token: str, content: str, ephemeral: bool = False):
        body = {"content": content, "allowed_mentions": NO_PINGS}
        if ephemeral:
            body["flags"] = EPHEMERAL
        return await self.request("POST", f"/webhooks/{application_id}/{token}", json=body, auth=False,
                                  route="POST /webhooks/{app}/{token}")

    # commands ----------------------------------------------------------------
    async def overwrite_commands(self, application_id: str, commands: list, guild_id: Optional[str] = None):
        path = (f"/applications/{application_id}/guilds/{guild_id}/commands" if guild_id
                else f"/applications/{application_id}/commands")
        return await self.request("PUT", path, json=commands, route="PUT commands")

    async def me(self):
        return await self.request("GET", "/users/@me")


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, (dict, list)) else {}
