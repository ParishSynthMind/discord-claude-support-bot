"""support-bot: register commands, print the invite link, check the setup, run the server,
and look at cases, reports and spend. Secrets are read from the environment or the Keychain
and are never printed."""
import argparse
import asyncio
import json
import sys

from .commands import COMMANDS, invite_url, validate
from .config import Settings
from .discord_api import DiscordAPI


def _settings(args) -> Settings:
    return Settings.from_env(use_keychain=not getattr(args, "no_keychain", False))


async def _register(s: Settings, scope_global: bool, transport=None) -> list:
    api = DiscordAPI(s.bot_token, s.api_base, transport=transport)
    try:
        return await api.overwrite_commands(s.application_id, COMMANDS, None if scope_global else s.guild_id)
    finally:
        await api.aclose()


def cmd_register(args, transport=None) -> int:
    problems = validate(COMMANDS)
    if problems:
        print("Command definitions break Discord's limits:\n  " + "\n  ".join(problems))
        return 2
    if args.dry_run:
        print(json.dumps(COMMANDS, indent=2))
        return 0
    s = _settings(args)
    if not s.application_id or not s.bot_token or (not args.global_ and not s.guild_id):
        print("Need DISCORD_APPLICATION_ID, the bot token and (for guild commands) DISCORD_GUILD_ID.")
        return 2
    saved = asyncio.run(_register(s, args.global_, transport))
    where = "globally" if args.global_ else f"in guild {s.guild_id}"
    print(f"Registered {len(saved)} commands {where}: " + ", ".join(c["name"] for c in saved))
    return 0


def cmd_invite(args) -> int:
    s = _settings(args)
    if not s.application_id:
        print("Set DISCORD_APPLICATION_ID first.")
        return 2
    print(invite_url(s.application_id, s.guild_id))
    return 0


def cmd_check(args, transport=None) -> int:
    s = _settings(args)
    problems = s.problems()
    if not problems and not args.offline:
        async def me():
            api = DiscordAPI(s.bot_token, s.api_base, transport=transport)
            try:
                return await api.me()
            finally:
                await api.aclose()
        try:
            user = asyncio.run(me())
            print(f"OK  bot token works (bot user {user.get('username')})")
        except Exception as exc:
            problems.append(f"Discord refused the bot token: {exc}")
    for p in problems:
        print(f"FIX {p}")
    if not problems:
        print("OK  everything needed for a live run is set")
    return 1 if problems else 0


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app
    from .signature import Verifier
    from .wiring import build
    s = _settings(args)
    problems = s.problems()
    if problems:
        print("Can't start:\n  " + "\n  ".join(problems))
        return 2
    bot = build(s)
    app = create_app(bot, Verifier(s.public_key, s.signature_max_age_s))
    if s.enable_gateway:
        from .gateway import Gateway

        @app.on_event("startup")
        async def start_gateway():
            gw = Gateway(s.bot_token, s.gateway_url, bot.on_gateway_event, s.message_content_intent)

            async def run():
                try:
                    await gw.run()
                except Exception as exc:
                    await bot.alerts.failure("gateway connection", exc)
            bot.later(run(), "gateway")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_ask(args) -> int:
    """Answer one question from the knowledge base locally (no Discord), to try the KB and prompt."""
    from .ai import ClaudeAI, wrap_question
    from .kb import KnowledgeBase
    s = _settings(args)
    kb = KnowledgeBase.load(s.kb_dir)
    hits = kb.search(args.question)
    if not hits:
        print("not_in_docs (no section matched, no model call made)")
        return 0
    if not s.anthropic_api_key:
        print("Anthropic key missing (Keychain item anthropic-api-key)")
        return 2
    ans = asyncio.run(ClaudeAI(s.anthropic_api_key, s.model, s.community, s.max_output_tokens).answer(args.question, hits))
    print(f"[{ans.status}] {ans.text}")
    for i in ans.citations:
        print(f"  source: {hits[i - 1].section.cite}")
    print(f"  tokens: {ans.tokens_in} in / {ans.tokens_out} out; prompt {len(wrap_question(args.question, hits))} chars")
    return 0


def cmd_kb_search(args) -> int:
    from .kb import KnowledgeBase
    kb = KnowledgeBase.load(_settings(args).kb_dir)
    for h in kb.search(args.query):
        print(f"{h.score:6.2f}  {h.section.cite}")
    return 0


def cmd_list(args) -> int:
    from .store import Store
    store = Store(_settings(args).db_path)
    if args.what == "spend":
        print(json.dumps(store.spend_today(), indent=2))
        return 0
    rows = store.cases() if args.what == "cases" else store.reports()
    for r in rows:
        if args.what == "cases":
            print(f"#{r['id']:<4} {r['status']:<12} {r['answer_status'] or '-':<12} {r['question'][:70]}")
        else:
            print(f"#{r['id']:<4} {r['status']:<10} {r['source']:<8} {r['category'] or '-':<10} {r['reason'][:60]}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="support-bot", description=__doc__)
    p.add_argument("--no-keychain", action="store_true", help="read secrets from the environment only")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="register the slash and message commands (bulk overwrite)")
    r.add_argument("--global", dest="global_", action="store_true", help="global commands instead of the test guild")
    r.add_argument("--dry-run", action="store_true", help="print the JSON and send nothing")
    sub.add_parser("invite-url", help="print the invite link with the minimal permissions")
    c = sub.add_parser("check", help="check every setting and that Discord accepts the token")
    c.add_argument("--offline", action="store_true", help="don't call Discord")
    s = sub.add_parser("serve", help="run the interactions endpoint")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    a = sub.add_parser("ask", help="answer one question from the knowledge base, without Discord")
    a.add_argument("question")
    k = sub.add_parser("kb-search", help="show which knowledge-base sections match")
    k.add_argument("query")
    li = sub.add_parser("list", help="show cases, reports or today's spend")
    li.add_argument("what", choices=["cases", "reports", "spend"])
    args = p.parse_args(argv)
    handlers = {"register": cmd_register, "invite-url": cmd_invite, "check": cmd_check, "serve": cmd_serve,
                "ask": cmd_ask, "kb-search": cmd_kb_search, "list": cmd_list}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
