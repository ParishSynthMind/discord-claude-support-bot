from support_bot.store import CASE_CLOSED, CASE_HANDED_OVER, CASE_OPEN, Store


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_interaction_claimed_once():
    s = Store()
    assert s.claim_interaction("1", "2") is True
    assert s.claim_interaction("1", "2") is False


def test_case_status_moves_are_atomic():
    s = Store()
    cid = s.open_case("g", "c", "u", "q")
    assert s.move_case(cid, CASE_HANDED_OVER, (CASE_OPEN,))
    assert not s.move_case(cid, CASE_HANDED_OVER, (CASE_OPEN,))   # second click does nothing
    assert s.move_case(cid, CASE_CLOSED, (CASE_OPEN, CASE_HANDED_OVER))
    assert s.case(cid)["status"] == CASE_CLOSED


def test_report_unique_per_reporter_and_message():
    s = Store()
    a = s.add_report(message_id="m", reporter_id="r1", source="member", reason="x", excerpt="")
    assert a and s.add_report(message_id="m", reporter_id="r1", source="member", reason="x", excerpt="") is None
    assert s.add_report(message_id="m", reporter_id="r2", source="member", reason="x", excerpt="")
    assert len(s.open_reports_for_message("m")) == 2


def test_report_resolved_once():
    s = Store()
    rid = s.add_report(message_id="m", reporter_id="r", source="member", reason="x", excerpt="")
    assert s.resolve_report(rid, "dismissed", "mod")
    assert not s.resolve_report(rid, "removed", "mod2")
    assert s.report(rid)["status"] == "dismissed"


def test_rate_limit_window_slides():
    clock = Clock()
    s = Store(clock=clock)
    assert all(s.hit("b", "u", 3, 60) is None for _ in range(3))
    wait = s.hit("b", "u", 3, 60)
    assert wait and 0 < wait <= 60
    assert s.hit("b", "other", 3, 60) is None   # per user
    clock.t += 61
    assert s.hit("b", "u", 3, 60) is None


def test_spend_reserve_respects_cap_and_settles():
    s = Store()
    assert s.reserve(0.6, 1.0)
    assert not s.reserve(0.5, 1.0)          # 0.6 held + 0.5 > 1.0
    s.settle(0.6, 0.01, 1000, 50)
    today = s.spend_today()
    assert round(today["spent"], 5) == 0.01 and today["reserved"] == 0 and today["calls"] == 1
    assert s.reserve(0.5, 1.0)


def test_spend_resets_next_utc_day():
    clock = Clock()
    s = Store(clock=clock)
    assert s.reserve(1.0, 1.0)
    s.settle(1.0, 1.0, 0, 0)
    assert not s.reserve(0.01, 1.0)
    clock.t += 86400
    assert s.reserve(0.01, 1.0)


def test_trail_records_events():
    s = Store()
    s.log("x", a=1)
    assert s.trail()[-1]["event"] == "x"
