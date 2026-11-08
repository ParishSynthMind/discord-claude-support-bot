"""What the bot does with each interaction.

Discord wants the first answer within 3 seconds, so `handle()` only does quick local work
(signature already checked, idempotency, rate limit, permission checks, a database write)
and returns the initial response. Anything slow (Discord API calls, Claude) runs afterwards
as a tracked background task that edits the original response or posts in the case thread.
Every background failure goes to Alerts and the member gets a plain message, never silence."""
import asyncio
import logging
from typing import Optional

from . import ai as ai_mod
from .commands import MANAGE_MESSAGES, MANAGE_THREADS
from .discord_api import EPHEMERAL, NO_PINGS
from .moderation import jump_link
from .store import CASE_CLOSED, CASE_HANDED_OVER, CASE_OPEN

log = logging.getLogger("support_bot.handlers")

PING, APPLICATION_COMMAND, MESSAGE_COMPONENT, AUTOCOMPLETE, MODAL_SUBMIT = 1, 2, 3, 4, 5
PONG, CHANNEL_MESSAGE, DEFERRED_CHANNEL_MESSAGE, DEFERRED_UPDATE, UPDATE_MESSAGE = 1, 4, 5, 6, 7
ADMINISTRATOR = 1 << 3
BUTTON, ACTION_ROW = 2, 1
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4


def reply(content: str, ephemeral: bool = True) -> dict:
    data = {"content": content, "allowed_mentions": NO_PINGS}
    if ephemeral:
        data["flags"] = EPHEMERAL
    return {"type": CHANNEL_MESSAGE, "data": data}


def buttons(*specs) -> list:
    return [{"type": ACTION_ROW, "components": [
        {"type": BUTTON, "style": style, "label": label, "custom_id": cid} for label, style, cid in specs]}]


def user_of(payload: dict) -> dict:
    return (payload.get("member") or {}).get("user") or payload.get("user") or {}


def has_perm(payload: dict, bit: int) -> bool:
    try:
        perms = int((payload.get("member") or {}).get("permissions") or 0)
    except ValueError:
        return False
    return bool(perms & ADMINISTRATOR) or bool(perms & bit)


def option(payload: dict, name: str) -> Optional[str]:
    for o in (payload.get("data") or {}).get("options") or []:
        if o.get("name") == name:
            return o.get("value")
    return None


def minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    if m >= 60 and m % 60 == 0:
        h = m // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


class SupportBot:
    def __init__(self, settings, store, kb, ai, api, alerts, screener=None):
        self.s, self.store, self.kb, self.ai, self.api, self.alerts = settings, store, kb, ai, api, alerts
        self.screener = screener
        self.tasks: set = set()

    # plumbing ----------------------------------------------------------------
    def later(self, coro, what: str) -> None:
        async def run():
            try:
                await coro
            except Exception as exc:  # reported, never swallowed
                await self.alerts.failure(what, exc)
        task = asyncio.get_running_loop().create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self, timeout: float = 30) -> None:
        """Wait for background work (tests, the local run and shutdown)."""
        while self.tasks:
            await asyncio.wait_for(asyncio.gather(*list(self.tasks), return_exceptions=True), timeout)

    async def spend_ok_call(self, prompt_chars: int, call):
        """Run one Claude call inside the daily cap. Returns None if the cap would be broken."""
        s = self.s
        hold = ai_mod.estimate_cost(prompt_chars, s.max_output_tokens, s.price_in_per_m, s.price_out_per_m)
        if not self.store.reserve(hold, s.daily_spend_cap_usd):
            self.store.log("spend_cap_reached", hold=round(hold, 5))
            return None
        try:
            result = await call()
        except Exception:
            self.store.settle(hold, 0.0, 0, 0)
            raise
        cost = ai_mod.actual_cost(result.tokens_in, result.tokens_out, s.price_in_per_m, s.price_out_per_m)
        self.store.settle(hold, cost, result.tokens_in, result.tokens_out)
        return result

    # entry point -------------------------------------------------------------
    async def handle(self, payload: dict) -> dict:
        kind = payload.get("type")
        if kind == PING:
            return {"type": PONG}
        iid = str(payload.get("id", ""))
        if not iid or not self.store.claim_interaction(iid, str(kind)):
            self.store.log("duplicate_interaction", id=iid)
            return reply("That request is already being handled.")
        data = payload.get("data") or {}
        if not payload.get("guild_id"):
            return reply("I only work inside the server.")
        if kind == APPLICATION_COMMAND:
            name = data.get("name")
            if name == "support":
                return self.cmd_support(payload)
            if name == "human":
                return self.cmd_human(payload)
            if name == "close":
                return self.cmd_close(payload)
            if name == "Report to moderators":
                return self.cmd_report(payload)
            return reply("I don't know that command yet.")
        if kind == MESSAGE_COMPONENT:
            return self.on_button(payload)
        return reply("That isn't supported here.")

    # /support ----------------------------------------------------------------
    def cmd_support(self, p: dict) -> dict:
        user = user_of(p).get("id", "")
        question = (option(p, "question") or "").strip()
        if not question:
            return reply("Please type your question after /support.")
        wait = self.store.hit("support", user, self.s.support_per_user, self.s.support_window_s)
        if wait:
            self.store.log("rate_limited", user=user, bucket="support")
            per = "hour" if self.s.support_window_s == 3600 else minutes(self.s.support_window_s)
            return reply(f"You've reached the limit of {self.s.support_per_user} questions per {per}. "
                         f"Please try again in {minutes(wait)}, or press Talk to a human in one of your case threads.")
        case_id = self.store.open_case(p.get("guild_id"), p.get("channel_id"), user, question)
        self.store.log("case_opened", case=case_id, user=user)
        self.later(self.answer_case(p, case_id, question, user), f"answering case #{case_id}")
        return {"type": DEFERRED_CHANNEL_MESSAGE}

    async def answer_case(self, p: dict, case_id: int, question: str, user: str) -> None:
        app, token = self.s.application_id, p["token"]
        try:
            thread = await self.api.start_thread(self.s.support_channel_id or p["channel_id"],
                                                 f"Case {case_id}: {question[:80]}")
        except Exception:
            await self.api.edit_original(app, token, "Sorry, I couldn't open a thread for your question. "
                                         "The moderators have been told.")
            raise
        thread_id = thread["id"]
        self.store.update_case(case_id, thread_id=thread_id)
        await self.api.send(thread_id, f"<@{user}> asked: {ai_mod.clean(question, 600)}",
                            allowed_mentions={"users": [user]})  # adds the asker to the thread
        await self.api.edit_original(app, token, f"Opened case #{case_id} for you: <#{thread_id}>")

        hits = self.kb.search(question)
        answer = None
        if not hits:
            answer = ai_mod.Answer("not_in_docs", ai_mod.NOT_IN_DOCS)  # nothing to ground on: no model call
        else:
            try:
                answer = await self.spend_ok_call(len(ai_mod.wrap_question(question, hits)),
                                                  lambda: self.ai.answer(question, hits))
            except Exception as exc:
                self.store.update_case(case_id, answer_status="error")
                await self.alerts.failure(f"Claude answer for case #{case_id}", exc)
                await self.api.send(thread_id, "Something went wrong on my side, so I've asked a moderator to help.")
                await self.handover(case_id, by="bot", why="the AI answer failed")
                return
            if answer is None:
                self.store.update_case(case_id, answer_status="spend_cap")
                await self.api.send(thread_id, "AI answers are paused for today (the daily spend limit was reached). "
                                    "I've asked a moderator to help.")
                await self.handover(case_id, by="bot", why="daily AI spend limit reached")
                return

        self.store.update_case(case_id, answer_status=answer.status)
        self.store.log("case_answered", case=case_id, status=answer.status, citations=answer.citations,
                       tokens_in=answer.tokens_in, tokens_out=answer.tokens_out)
        if answer.status == "answered":
            sources = "; ".join(hits[i - 1].section.cite for i in answer.citations)
            await self.api.send(thread_id, f"{answer.text}\n\n-# Sources: {sources}",
                                components=buttons(("Solved", SUCCESS, f"solved:{case_id}"),
                                                   ("Talk to a human", SECONDARY, f"human:{case_id}")))
        elif answer.status == "not_in_docs":
            await self.api.send(thread_id, f"{answer.text} Press Talk to a human and a moderator will help.",
                                components=buttons(("Talk to a human", PRIMARY, f"human:{case_id}")))
        else:
            await self.api.send(thread_id, answer.text)
            await self.handover(case_id, by="bot", why="the question needs a person")

    # handover ----------------------------------------------------------------
    async def handover(self, case_id: int, by: str, why: str) -> bool:
        if not self.store.move_case(case_id, CASE_HANDED_OVER, (CASE_OPEN,), handed_over_by=by):
            return False
        case = self.store.case(case_id)
        role = self.s.mod_role_id
        ping = f"<@&{role}> " if role else ""
        await self.api.send(
            self.s.mod_channel_id,
            f"{ping}Case #{case_id} needs a person: <#{case['thread_id']}>\n"
            f"Asked by <@{case['user_id']}>: {ai_mod.clean(case['question'], 300)}\nWhy: {why}",
            components=buttons(("Close case", SECONDARY, f"close:{case_id}")),
            allowed_mentions={"roles": [role]} if role else NO_PINGS)
        if case.get("thread_id"):
            await self.api.send(case["thread_id"], "A moderator has been asked to join. "
                                "I'll stay out of this case now so they can help you directly.")
        self.store.log("case_handed_over", case=case_id, by=by, why=why)
        return True

    def can_act_on_case(self, p: dict, case: dict) -> bool:
        return user_of(p).get("id") == case["user_id"] or has_perm(p, MANAGE_THREADS)

    def cmd_human(self, p: dict) -> dict:
        case = self.store.case_by_thread(p.get("channel_id", ""))
        if not case:
            return reply("Use /human inside one of your support case threads.")
        return self._human(p, case)

    def _human(self, p: dict, case: dict) -> dict:
        if not self.can_act_on_case(p, case):
            return reply("Only the person who asked, or a moderator, can do that.")
        if case["status"] != CASE_OPEN:
            return reply("This case is already with a moderator." if case["status"] == CASE_HANDED_OVER
                         else "This case is closed. Use /support to open a new one.")
        self.later(self.handover(case["id"], by=user_of(p).get("id", ""), why="the member asked for a person"),
                   f"handing over case #{case['id']}")
        return reply("Done. A moderator has been asked to help in this thread.")

    def cmd_close(self, p: dict) -> dict:
        if not has_perm(p, MANAGE_THREADS):
            return reply("Only moderators can close a case.")
        case = self.store.case_by_thread(p.get("channel_id", ""))
        if not case:
            return reply("Use /close inside a support case thread.")
        mod = user_of(p).get("id", "")
        if not self.store.move_case(case["id"], CASE_CLOSED, (CASE_OPEN, CASE_HANDED_OVER), closed_by=mod):
            return reply("This case is already closed.")
        self.store.log("case_closed", case=case["id"], by=mod)
        self.later(self.api.archive_thread(case["thread_id"], f"Case {case['id']} closed by moderator {mod}"),
                   f"archiving case #{case['id']}")
        return reply(f"Case #{case['id']} closed by <@{mod}>. Thanks!", ephemeral=False)

    # buttons -----------------------------------------------------------------
    def on_button(self, p: dict) -> dict:
        cid = (p.get("data") or {}).get("custom_id", "")
        action, _, ref = cid.partition(":")
        if not ref.isdigit():
            return reply("That button has expired.")
        ref_id = int(ref)
        if action in ("solved", "human", "close"):
            case = self.store.case(ref_id)
            if not case:
                return reply("That case no longer exists.")
            if action == "human":
                return self._human(p, case)
            if action == "solved":
                return self._solved(p, case)
            return self._close_from_queue(p, case)
        if action in ("dismiss", "remove"):
            return self._resolve_report(p, ref_id, action)
        return reply("That button has expired.")

    def _strip_buttons(self, p: dict, note: str) -> dict:
        old = (p.get("message") or {}).get("content", "")
        return {"type": UPDATE_MESSAGE, "data": {"content": f"{old}\n\n{note}"[:2000], "components": [],
                                                 "allowed_mentions": NO_PINGS}}

    def _solved(self, p: dict, case: dict) -> dict:
        if not self.can_act_on_case(p, case):
            return reply("Only the person who asked, or a moderator, can mark this solved.")
        who = user_of(p).get("id", "")
        if not self.store.move_case(case["id"], CASE_CLOSED, (CASE_OPEN, CASE_HANDED_OVER), closed_by=who):
            return reply("This case is already closed.")
        self.store.log("case_solved", case=case["id"], by=who)
        self.later(self.api.archive_thread(case["thread_id"], f"Case {case['id']} marked solved"),
                   f"archiving case #{case['id']}")
        return self._strip_buttons(p, f"Marked solved by <@{who}>. This thread will be archived.")

    def _close_from_queue(self, p: dict, case: dict) -> dict:
        if not has_perm(p, MANAGE_THREADS):
            return reply("Only moderators can close a case.")
        mod = user_of(p).get("id", "")
        if not self.store.move_case(case["id"], CASE_CLOSED, (CASE_OPEN, CASE_HANDED_OVER), closed_by=mod):
            return reply("This case is already closed.")
        self.store.log("case_closed", case=case["id"], by=mod)

        async def finish():
            await self.api.send(case["thread_id"], "A moderator closed this case. Use /support if you need anything else.")
            await self.api.archive_thread(case["thread_id"], f"Case {case['id']} closed by moderator {mod}")
        self.later(finish(), f"closing case #{case['id']}")
        return self._strip_buttons(p, f"Closed by <@{mod}>.")

    # reports -----------------------------------------------------------------
    def cmd_report(self, p: dict) -> dict:
        data = p.get("data") or {}
        target = data.get("target_id", "")
        msg = ((data.get("resolved") or {}).get("messages") or {}).get(target)
        if not msg:
            return reply("I couldn't read that message. Please tell a moderator directly.")
        reporter = user_of(p).get("id", "")
        author = (msg.get("author") or {}).get("id", "")
        if author == self.s.application_id or (msg.get("author") or {}).get("bot"):
            return reply("That's a bot message. If it's wrong, press Talk to a human in the case thread.")
        wait = self.store.hit("report", reporter, self.s.reports_per_user, self.s.reports_window_s)
        if wait:
            return reply(f"You've sent a lot of reports. Please try again in {minutes(wait)} "
                         "or message a moderator directly.")
        report_id = self.store.add_report(
            guild_id=p.get("guild_id"), channel_id=msg.get("channel_id") or p.get("channel_id"), message_id=target,
            author_id=author, reporter_id=reporter, source="member", reason="reported by a member",
            excerpt=(msg.get("content") or "")[:1000])
        if report_id is None:
            return reply("You already reported this message. The moderators have it.")
        self.store.log("report_opened", report=report_id, source="member")
        self.later(self.post_report(report_id), f"posting report #{report_id}")
        return reply("Thanks. The moderators have been told. Nothing is removed until a person has looked at it.")

    async def on_gateway_event(self, msg: dict) -> None:
        """Called from the gateway loop. Returns at once: the screening, the Claude label and
        the post run as tracked background work, so heartbeats are never held up."""
        self.later(self.on_gateway_message(msg), f"flagging gateway message {msg.get('id')}")

    async def on_gateway_message(self, msg: dict) -> Optional[int]:
        """MESSAGE_CREATE from the optional gateway connection: flag, never delete."""
        if not self.screener:
            return None
        why = self.screener.screen(msg)
        if not why:
            return None
        report_id = self.store.add_report(
            guild_id=msg.get("guild_id"), channel_id=msg.get("channel_id"), message_id=msg.get("id"),
            author_id=(msg.get("author") or {}).get("id", ""), reporter_id="auto-rules", source="gateway",
            reason=why, excerpt=(msg.get("content") or "")[:1000])
        if report_id is None:
            return None
        self.store.log("report_opened", report=report_id, source="gateway", why=why)
        await self.post_report(report_id)
        return report_id

    async def post_report(self, report_id: int) -> None:
        r = self.store.report(report_id)
        label = ""
        if self.s.triage_reports and r["excerpt"]:
            try:
                t = await self.spend_ok_call(len(r["excerpt"]) + 600, lambda: self.ai.triage(r["excerpt"], r["reason"]))
            except Exception as exc:
                t = None
                await self.alerts.failure(f"AI label for report #{report_id}", exc)
            if t:
                self.store.update_report(report_id, category=t.category, severity=t.severity)
                label = f"\nAI label (a suggestion only): {t.category}, {t.severity}. {t.reason}"
        others = len(self.store.open_reports_for_message(r["message_id"]))
        count = f" ({others} open reports on this message)" if others > 1 else ""
        who = "Automatic rule" if r["source"] == "gateway" else f"<@{r['reporter_id']}>"
        excerpt = ai_mod.clean(r["excerpt"], 300) or "(no text: the message content isn't available to the bot)"
        sent = await self.api.send(
            self.s.mod_channel_id,
            f"Report #{report_id}{count}: {who} flagged a message by <@{r['author_id']}> in <#{r['channel_id']}>\n"
            f"Why: {r['reason']}\n> {excerpt}\n{jump_link(r['guild_id'], r['channel_id'], r['message_id'])}{label}",
            components=buttons(("Dismiss", SECONDARY, f"dismiss:{report_id}"),
                               ("Remove message", DANGER, f"remove:{report_id}")))
        self.store.update_report(report_id, mod_message_id=(sent or {}).get("id"))

    def _resolve_report(self, p: dict, report_id: int, action: str) -> dict:
        if not has_perm(p, MANAGE_MESSAGES):
            return reply("Only moderators can act on reports.")
        r = self.store.report(report_id)
        if not r:
            return reply("That report no longer exists.")
        mod = user_of(p).get("id", "")
        status = "removed" if action == "remove" else "dismissed"
        if not self.store.resolve_report(report_id, status, mod):
            return reply(f"Report #{report_id} was already handled.")
        self.store.log("report_" + status, report=report_id, by=mod)
        if action == "remove":
            async def remove():
                try:
                    await self.api.delete_message(r["channel_id"], r["message_id"],
                                                  f"Removed by moderator {mod} after report {report_id}")
                except Exception as exc:
                    if getattr(exc, "code", None) == 10008:  # Unknown Message: already gone
                        self.store.log("report_message_already_gone", report=report_id)
                        return
                    self.store.update_report(report_id, status="open", resolved_by=None)  # not removed after all
                    raise
            self.later(remove(), f"removing the message in report #{report_id}")
            return self._strip_buttons(p, f"Removed by <@{mod}>.")
        return self._strip_buttons(p, f"Dismissed by <@{mod}>. No action taken.")
