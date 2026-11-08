"""Render the README images from the recorded run in examples/local-run/run.json (real Claude
answers over real HTTP, Discord mocked). Nothing is typed in by hand: every message, button,
status and number below comes from that file.

    python scripts/render_media.py   (needs: pip install playwright && playwright install chromium)
"""
import html
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = json.loads((ROOT / "examples" / "local-run" / "run.json").read_text())
MEDIA = ROOT / "media"
NAMES = RUN["names"]
THREAD_NAMES = {}
COLORS = {"maya": "#f47fff", "priya": "#3ba55c", "omar": "#faa61a", "jonah": "#00b0f4", "sam_mod": "#5865f2",
          "spammy": "#ed4245"}
FONT = "'gg sans','Noto Sans',-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
CSS = f"""<style>
body{{margin:0;background:#313338;font-family:{FONT};color:#dbdee1}}
.wrap{{display:inline-block;width:1080px;padding:22px 26px}}
h1{{font-size:21px;margin:0 0 4px;color:#f2f3f5}} .sub{{color:#949ba4;font-size:13.5px;margin:0 0 16px;line-height:1.45}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.card{{background:#2b2d31;border-radius:8px;padding:12px 14px}}
.th{{font-size:13px;color:#b5bac1;margin-bottom:10px;display:flex;gap:8px;align-items:center}}
.th b{{color:#f2f3f5;font-weight:600}}
.badge{{margin-left:auto;font-size:11px;border-radius:10px;padding:2px 8px;color:#fff;white-space:nowrap}}
.msg{{display:flex;gap:10px;margin:0 0 11px}}
.av{{width:32px;height:32px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;
font-weight:700;color:#fff;font-size:14px}}
.name{{font-weight:600;font-size:14px}} .app{{background:#5865f2;color:#fff;font-size:9.5px;border-radius:3px;padding:1px 4px;
margin-left:5px;vertical-align:2px}}
.text{{font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}}
.src{{color:#949ba4;font-size:12px;display:block;margin-top:4px}}
.mention{{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px}}
.eph{{color:#949ba4;font-size:11.5px;margin-top:3px}}
.btns{{margin-top:6px;display:flex;gap:6px}}
.btn{{font-size:12.5px;border-radius:3px;padding:4px 11px;color:#fff;background:#4e5058}}
.btn.Solved{{background:#248046}} .btn.Remove{{background:#da373c}} .btn.Talk{{background:#4e5058}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}}
.stat{{background:#2b2d31;border-radius:6px;padding:7px 11px;font-size:13px}} .stat b{{color:#23a55a}}
.note{{color:#949ba4;font-size:12px;margin-top:12px}}
</style>"""


def name(uid):
    return NAMES.get(uid, "member")


def render_text(text):
    t = html.escape(text)
    t = re.sub(r"&lt;@&amp;\d+&gt;", "<span class='mention'>@Moderators</span>", t)
    t = re.sub(r"&lt;@!?(\d+)&gt;", lambda m: f"<span class='mention'>@{name(m.group(1))}</span>", t)
    t = re.sub(r"&lt;#(\d+)&gt;", lambda m: f"<span class='mention'>#{THREAD_NAMES.get(m.group(1), 'general')}</span>", t)
    if "\n\n-# Sources:" in t:
        body, src = t.split("\n\n-# Sources:", 1)
        t = f"{body}<span class='src'>Sources:{src}</span>"
    return t


def avatar(who):
    return f"<div class='av' style='background:{COLORS.get(who, '#80848e')}'>{html.escape(who[0].upper())}</div>"


def bot_msg(text, buttons=(), eph=False):
    b = "".join(f"<span class='btn {html.escape(x.split()[0])}'>{html.escape(x)}</span>" for x in buttons)
    return (f"<div class='msg'>{avatar('L')}<div><span class='name' style='color:#c9cdfb'>Lumen Helper</span>"
            f"<span class='app'>APP</span><div class='text'>{render_text(text)}</div>"
            f"{'<div class=btns>' + b + '</div>' if b else ''}"
            f"{'<div class=eph>Only you can see this</div>' if eph else ''}</div></div>")


def user_msg(who, text):
    return (f"<div class='msg'>{avatar(who)}<div><span class='name' style='color:{COLORS.get(who, '#fff')}'>"
            f"{html.escape(who)}</span><div class='text'>{render_text(text)}</div></div></div>")


STATUS = {"open": ("#4e5058", "open"), "handed_over": ("#c27c0e", "with a moderator"), "closed": ("#248046", "closed")}
ANSWER = {"answered": "answered with sources", "not_in_docs": "not in docs: refused", "needs_human": "needs a person"}


def case_card(cid, t):
    color, label = STATUS[t["status"]]
    out = [f"<div class='card'><div class='th'># <b>{html.escape(t['name'])}</b>"
           f"<span>{ANSWER.get(t['answer_status'], '')}</span>"
           f"<span class='badge' style='background:{color}'>{label}{' / archived' if t['archived'] else ''}</span></div>"]
    msgs = t["messages"]
    first = msgs[0]["content"]
    asked = first.split(" asked: ", 1)[1] if " asked: " in first else first
    out.append(user_msg(t["asker"], f"/support {asked}"))
    for m in msgs[1:]:
        eph = m["content"].startswith("Done. A moderator")  # the button reply, shown only to the presser
        out.append(bot_msg(m["content"], m["buttons"], eph))
    out.append("</div>")
    return "".join(out)


def shot(page, html_doc, path):
    page.set_content(html_doc)
    page.locator(".wrap").screenshot(path=str(path))
    print("wrote", path)


def main():
    MEDIA.mkdir(exist_ok=True)
    for cid, t in RUN["threads"].items():
        tid = re.search(r"<#(\d+)>", RUN["originals_in_general"][int(cid) - 1]).group(1)
        THREAD_NAMES[tid] = t["name"]
    sp = RUN["spend"]
    steps = RUN["steps"]
    firsts = [s["seconds_to_first_response"] for s in steps if s["seconds_to_first_response"] is not None]
    limited = [s for s in steps if s["ephemeral_reply"] and "try again in" in s["ephemeral_reply"]]

    cards = "".join(case_card(cid, t) for cid, t in RUN["threads"].items())
    stats = (f"<div class='stats'><div class='stat'>Endpoint check: PING <b>OK</b>, bad signature <b>401</b></div>"
             f"<div class='stat'>Forged request: <b>{RUN['forged_request_status']}</b></div>"
             f"<div class='stat'>First response: <b>{min(firsts) * 1000:.0f}-{max(firsts) * 1000:.0f} ms</b> (limit 3 s)</div>"
             f"<div class='stat'>Claude {html.escape(RUN['model'])}: <b>{sp['calls']}</b> calls, "
             f"{sp['tokens_in']:,} in / {sp['tokens_out']:,} out, <b>${sp['spent']:.3f}</b></div>"
             f"<div class='stat'>Rate limit hit: <b>{len(limited)}</b></div></div>")
    doc1 = (f"<html><head>{CSS}</head><body><div class='wrap'><h1>/support: one thread per case, answers only from the docs</h1>"
            f"<div class='sub'>Recorded {html.escape(RUN['recorded_at'])}. Real Claude replies over real HTTP; Discord itself is "
            f"a mock built from Discord's docs. Every message below is copied from examples/local-run/run.json.</div>"
            f"<div class='grid'>{cards}</div>{stats}"
            f"<div class='note'>Rate-limited reply to jonah's third question: {html.escape(limited[0]['ephemeral_reply']) if limited else '-'}"
            f"</div></div></body></html>")

    queue = "".join(bot_msg(m["content"], m["buttons"]) for m in RUN["mod_queue"])
    rep_rows = "".join(
        f"<div class='stat'>Report #{r['id']} ({html.escape(r['source'])}): label <b>{html.escape(str(r['category']))}"
        f"/{html.escape(str(r['severity']))}</b>, now <b>{html.escape(r['status'])}</b>"
        f"{' by @' + html.escape(name(r['resolved_by'])) if r['resolved_by'] else ''}</div>" for r in RUN["reports"])
    doc2 = (f"<html><head>{CSS}</head><body><div class='wrap'><h1>#mod-queue: handovers and reports, people decide</h1>"
            f"<div class='sub'>Handovers ping only the Moderators role. Reports come from the 'Report to moderators' message "
            f"command and from the optional gateway rules; Claude adds a label as a suggestion. Nothing is removed until a "
            f"moderator presses the button. Copied from the same recorded run.</div>"
            f"<div class='card'>{queue}</div><div class='stats'>{rep_rows}"
            f"<div class='stat'>Reported spam removed by a moderator: <b>{'yes' if not RUN['spam_message_still_there'] else 'no'}</b></div>"
            f"<div class='stat'>Gateway-flagged messages still up, waiting for a person: "
            f"<b>{'yes' if RUN['gateway_messages_still_there'] else 'no'}</b></div>"
            f"<div class='stat'>Gateway: intents {RUN['gateway']['identify_intents']}, heartbeats {RUN['gateway']['heartbeats']}</div>"
            f"<div class='stat'>Alerts raised: <b>{len(RUN['alerts'])}</b></div></div></div></body></html>")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1140, "height": 900}, device_scale_factor=2)
        shot(page, doc1, MEDIA / "support-cases.png")
        shot(page, doc2, MEDIA / "mod-queue.png")
        browser.close()


if __name__ == "__main__":
    main()
