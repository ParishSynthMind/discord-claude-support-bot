"""The HTTP side: Discord POSTs every interaction to /interactions. The raw body is checked
against the Ed25519 signature before anything is parsed; a bad or missing signature is a 401
(Discord tests this on purpose and removes the endpoint if bad requests are accepted)."""
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .signature import BadSignature, Verifier

log = logging.getLogger("support_bot.app")
MAX_BODY = 1_000_000


def create_app(bot, verifier: Verifier) -> FastAPI:
    app = FastAPI(title="Discord Claude support bot", docs_url=None, redoc_url=None)
    app.state.bot = bot

    @app.post("/interactions")
    async def interactions(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            verifier.check(request.headers.get("X-Signature-Ed25519"), request.headers.get("X-Signature-Timestamp"), body)
        except BadSignature as exc:
            bot.store.log("bad_signature", reason=str(exc))
            return JSONResponse({"error": "invalid request signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        try:
            response = await bot.handle(payload)
        except Exception as exc:
            await bot.alerts.failure(f"handling interaction {payload.get('id')}", exc)
            response = {"type": 4, "data": {"content": "Sorry, something went wrong. The moderators have been told.",
                                            "flags": 64, "allowed_mentions": {"parse": []}}}
        return JSONResponse(response)

    @app.get("/health")
    async def health():
        spend = bot.store.spend_today()
        return {"ok": True, "background_tasks": len(bot.tasks),
                "spend_today_usd": round(spend["spent"], 5), "cap_usd": bot.s.daily_spend_cap_usd}

    return app
