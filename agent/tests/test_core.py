"""Unit tests for agent/core.py — run from agent/:  python3 -m unittest"""
import json
import unittest
from datetime import datetime, timedelta

import core
from core import (DORMANT, IN_SESSION, LOCKED, RESERVED, TEACHER, Block, tick)

MON = "2026-11-09"          # a Monday
FRI = "2026-11-13"
SAT = "2026-11-14"
SUN = "2026-11-15"


def at(hhmm: str, d: str = MON, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.fromisoformat(d) + timedelta(hours=h, minutes=m, seconds=sec)


ROSTER = [
    {"rosterId": "r01", "displayName": "Maya R.", "class": "A"},
    {"rosterId": "r02", "displayName": "Sam K.", "class": "A"},
    {"rosterId": "r03", "displayName": "Lee P.", "class": "B"},
]


def sched(d=MON, slots=None, day_blocked=False, class_blocked=(), labels=None):
    """slots: {("A", 3): ("booked", "Maya R.") | "blocked" | ...}; labels: {"A": "CD", ...}"""
    slots = slots or {}
    classes = []
    for cls, base in core.BLOCK_START_MIN.items():
        ss = []
        for i in range(1, core.BLOCK_SLOTS[cls] + 1):
            v = slots.get((cls, i), "open")
            status, name = (v, None) if isinstance(v, str) else v
            m = base + (i - 1) * 15
            s = {"i": i, "start": f"{m // 60:02d}:{m % 60:02d}", "status": status}
            if name:
                s["displayName"] = name
            ss.append(s)
        entry = {"class": cls, "blocked": cls in class_blocked, "slots": ss}
        if labels:
            entry["label"] = labels[cls]
        classes.append(entry)
    return {"ok": True, "roster": ROSTER, "days": [{"date": d, "blocked": day_blocked, "classes": classes}]}


def cache(s, fetch_date=MON):
    return {"fetchDate": fetch_date, "data": s}


def roundtrip(state):
    """session.json survives a crash: JSON in, JSON out."""
    return json.loads(json.dumps(state))


class SlotMath(unittest.TestCase):
    def test_slot_starts(self):
        a = Block("A", MON, core.BLOCK_START_MIN["A"])
        b = Block("B", MON, core.BLOCK_START_MIN["B"])
        self.assertEqual([a.slot_start(i).strftime("%H:%M") for i in range(1, 7)],
                         ["07:30", "07:45", "08:00", "08:15", "08:30", "08:45"])
        self.assertEqual([b.slot_start(i).strftime("%H:%M") for i in range(1, 7)],
                         ["13:00", "13:15", "13:30", "13:45", "14:00", "14:15"])
        self.assertEqual(a.end, at("09:00"))
        self.assertEqual(b.end, at("14:30"))

    def test_slot_at_boundaries(self):
        a = Block("A", MON, core.BLOCK_START_MIN["A"])
        self.assertIsNone(a.slot_at(at("07:29", sec=59)))
        self.assertEqual(a.slot_at(at("07:30")), 1)
        self.assertEqual(a.slot_at(at("07:44", sec=59)), 1)
        self.assertEqual(a.slot_at(at("07:45")), 2)
        self.assertEqual(a.slot_at(at("08:59", sec=59)), 6)
        self.assertIsNone(a.slot_at(at("09:00")))

    def test_armed_windows(self):
        self.assertIsNone(core.active_block(at("07:24", sec=59)))
        self.assertEqual(core.active_block(at("07:25")).cls, "A")
        self.assertEqual(core.active_block(at("09:04", sec=59)).cls, "A")
        self.assertIsNone(core.active_block(at("09:05")))
        self.assertIsNone(core.active_block(at("12:54", sec=59)))
        self.assertEqual(core.active_block(at("12:55")).cls, "B")
        self.assertEqual(core.active_block(at("14:34", sec=59)).cls, "B")
        self.assertIsNone(core.active_block(at("14:35")))

    def test_fri_sat_weekend_dormant_sunday_is_a_school_day(self):
        for d in (FRI, SAT):
            self.assertIsNone(core.active_block(at("08:00", d)))
            self.assertEqual(tick(at("08:00", d), None, None).view.mode, DORMANT)
        self.assertEqual(core.active_block(at("08:00", SUN)).cls, "A")
        self.assertEqual(tick(at("13:10", SUN), None, None).view.mode, LOCKED)

    def test_next_armed_start(self):
        self.assertEqual(core.next_armed_start(at("10:00")), at("10:35"))          # lunch
        self.assertEqual(core.next_armed_start(at("11:15")), at("12:55"))
        self.assertEqual(core.next_armed_start(at("15:00", "2026-11-12")), at("07:25", SUN))   # Thu → Sun


class Lunch(unittest.TestCase):
    def blk(self):
        return Block("L", MON, core.BLOCK_START_MIN["L"])

    def test_two_slots_and_armed_window(self):
        self.assertEqual([self.blk().slot_start(i).strftime("%H:%M") for i in (1, 2)], ["10:40", "10:55"])
        self.assertEqual(self.blk().end, at("11:10"))
        self.assertIsNone(core.active_block(at("10:34", sec=59)))
        self.assertEqual(core.active_block(at("10:35")).cls, "L")
        self.assertEqual(core.active_block(at("11:14", sec=59)).cls, "L")
        self.assertIsNone(core.active_block(at("11:15")))

    def test_overtime_stops_at_lunch_end(self):
        self.assertEqual(core.compute_session_end(self.blk(), sched(), 1), at("11:10"))

    def test_walkup_list_is_everyone(self):
        names = [p["displayName"] for p in core.roster_for(cache(sched()), "L")]
        self.assertEqual(names, ["Lee P.", "Maya R.", "Sam K."])

    def test_student_from_any_class_is_reserved_and_no_show_names_them(self):
        c = cache(sched(slots={("L", 2): ("booked", "Lee P.")}))      # Lee P. is in class B
        r = tick(at("10:55"), None, c)
        self.assertEqual((r.view.mode, r.view.reserved_name), (RESERVED, "Lee P."))
        r = tick(at("11:00"), roundtrip(r.state), c)
        ns = [e for e in r.events if e["type"] == "no_show"]
        self.assertEqual([(e["class"], e["slot"], e["rosterId"]) for e in ns], [("L", 2, "r03")])

    def test_labels_and_slot_codes(self):
        c = cache(sched(labels={"A": "CD", "L": "Lunch", "B": "EF"}), "2026-11-01")   # any date
        self.assertEqual((core.block_label(c, "A"), core.slot_code(c, "A", 3)), ("CD", "CD3"))
        self.assertEqual(core.slot_code(c, "L", 1), "Lunch 1")
        self.assertEqual((core.block_label(None, "B"), core.slot_code(None, "B", 2)), ("Block B", "B2"))
        self.assertEqual(core.slot_code(None, "L", 2), "Lunch 2")
        p = cache(sched(labels={"A": "P1", "L": "Lunch", "B": "Period 5"}))
        self.assertEqual((core.slot_code(p, "A", 3), core.slot_code(p, "B", 2)), ("P1 3", "Period 5 2"))

    def test_lunch_test_window(self):
        tw, errs = core.parse_test_windows([{"block": "l", "start": "16:00"}])
        self.assertEqual(errs, [])
        self.assertEqual(core.active_block(at("16:10"), tw).end, at("16:30"))


class TestWindows(unittest.TestCase):
    def test_parse(self):
        ok, errs = core.parse_test_windows([
            {"block": "b", "start": "16:00"},
            {"block": "A", "start": "10:30", "slotMinutes": 6, "date": MON},
            {"block": "C", "start": "10:00"},
            {"start": "10:00"},
        ])
        self.assertEqual(len(ok), 2)
        self.assertEqual(len(errs), 2)
        self.assertEqual(ok[0], {"cls": "B", "start_min": 960, "slot_minutes": 15, "date": None})

    def test_test_window_arms_with_badge_and_maps_real_bookings(self):
        tw, _ = core.parse_test_windows([{"block": "B", "start": "16:00"}])
        s = sched(slots={("B", 2): ("booked", "Lee P.")})
        r = tick(at("16:16"), None, cache(s), tw)
        self.assertEqual(r.view.mode, RESERVED)
        self.assertTrue(r.view.test)
        self.assertEqual(r.view.reserved_name, "Lee P.")
        self.assertIn("TEST", core.badges(test=r.view.test, offline=False, drift_sec=None, events_failing=False,
                                          pending=0, oldest_queued=None, now=r.view.now))

    def test_test_windows_are_additive_and_never_override_real_blocks(self):
        tw, _ = core.parse_test_windows([{"block": "B", "start": "08:00"}, {"block": "A", "start": "18:00", "date": SAT}])
        b = core.active_block(at("08:10"), tw)
        self.assertEqual((b.cls, b.test), ("A", False))              # overlapping test window dropped
        self.assertTrue(core.active_block(at("18:10", SAT), tw).test)  # dated window only on its date
        self.assertIsNone(core.active_block(at("18:10", "2026-11-15"), tw))

    def test_short_test_slots(self):
        tw, _ = core.parse_test_windows([{"block": "A", "start": "16:00", "slotMinutes": 3}])
        b = core.active_block(at("16:07"), tw)
        self.assertEqual(b.slot_at(at("16:07")), 3)
        self.assertEqual(b.end, at("16:18"))


class CacheValidity(unittest.TestCase):
    def test_stale_cache_arms_locked_walkup_only_never_reserved(self):
        s = sched(d="2026-11-06", slots={("A", 3): ("booked", "Maya R.")})
        for t in ("08:00", "08:06", "08:14"):
            r = tick(at(t), None, cache(s, fetch_date="2026-11-06"))
            self.assertEqual(r.view.mode, LOCKED)
            self.assertTrue(r.view.walkup_allowed)
            self.assertFalse(r.view.schedule_known)
            self.assertEqual(r.events, [])

    def test_absent_cache_is_locked_offline(self):
        r = tick(at("07:31"), None, None)
        self.assertEqual(r.view.mode, LOCKED)
        self.assertTrue(r.view.walkup_allowed)
        self.assertTrue(core.is_offline(0, r.view.schedule_known))

    def test_yesterdays_session_state_is_ignored(self):
        st = core.fresh_state("2026-11-06")
        st["teacher"] = {"windowId": "x", "since": "2026-11-06T08:00:00"}
        r = tick(at("08:00"), st, cache(sched()))
        self.assertEqual(r.state["date"], MON)
        self.assertEqual(r.view.mode, LOCKED)

    def test_roster_from_stale_cache(self):
        names = [p["displayName"] for p in core.roster_for(cache(sched(), "2026-11-01"), "A")]
        self.assertEqual(names, ["Maya R.", "Sam K."])


class NoShow(unittest.TestCase):
    def setUp(self):
        self.c = cache(sched(slots={("A", 3): ("booked", "Maya R.")}))

    def run_until(self, start, end, state=None, step=1):
        t, st, events, last = start, state, [], None
        while t <= end:
            r = tick(t, roundtrip(st) if st else st, self.c)
            st, last = r.state, r
            events += [(t, e) for e in r.events]
            t += timedelta(seconds=step)
        return st, events, last

    def test_fires_at_slot_start_plus_5_when_displayed_from_start(self):
        st, events, last = self.run_until(at("07:59"), at("08:06"))
        no_shows = [(t, e) for t, e in events if e["type"] == "no_show"]
        self.assertEqual(len(no_shows), 1, "posted exactly once")
        t, e = no_shows[0]
        self.assertEqual(t, at("08:05"))
        self.assertEqual((e["date"], e["class"], e["slot"], e["rosterId"]), (MON, "A", 3, "r01"))
        self.assertEqual(last.view.mode, LOCKED)
        self.assertTrue(last.view.walkup_allowed)

    def test_reserved_until_deadline(self):
        r = tick(at("08:00"), None, self.c)
        self.assertEqual(r.view.mode, RESERVED)
        r = tick(at("08:04", sec=59), r.state, self.c)
        self.assertEqual(r.view.mode, RESERVED)
        self.assertEqual(r.view.no_show_at, at("08:05"))
        self.assertEqual(r.events, [])

    def test_late_first_display_shifts_deadline(self):
        # Kiosk first showed RESERVED at 08:03 (e.g. booted late) → no-show at 08:08, not 08:05.
        st, events, _ = self.run_until(at("08:03"), at("08:09"))
        times = [t for t, e in events if e["type"] == "no_show"]
        self.assertEqual(times, [at("08:08")])

    def test_deadline_past_slot_end_never_fires(self):
        st, events, last = self.run_until(at("08:11"), at("08:14", sec=59))
        self.assertEqual([e for _, e in events if e["type"] == "no_show"], [])
        self.assertEqual(last.view.mode, RESERVED)
        self.assertIsNone(last.view.no_show_at)
        r = tick(at("08:15"), st, self.c)                 # slot over: row stays booked, inert
        self.assertEqual(r.view.slot, 4)
        self.assertEqual(r.events, [])

    def test_first_seen_survives_restart(self):
        r = tick(at("08:02"), None, self.c)
        r2 = tick(at("08:07", sec=30), roundtrip(r.state), self.c)   # agent was dead 08:02–08:07:30
        self.assertEqual([e["type"] for e in r2.events], ["no_show"])

    def test_no_show_released_slot_stays_released_after_poll_lag(self):
        st, _, _ = self.run_until(at("08:00"), at("08:05"))
        r = tick(at("08:06"), st, self.c)                 # schedule still says booked
        self.assertEqual(r.view.mode, LOCKED)
        self.assertEqual(r.events, [])


class SessionEnd(unittest.TestCase):
    def block_a(self):
        return Block("A", MON, core.BLOCK_START_MIN["A"])

    def test_overtime_over_empty_slots_to_next_booking(self):
        s = sched(slots={("A", 5): ("booked", "Sam K.")})
        self.assertEqual(core.compute_session_end(self.block_a(), s, 2), at("08:30"))

    def test_overtime_to_block_end(self):
        self.assertEqual(core.compute_session_end(self.block_a(), sched(), 2), at("09:00"))

    def test_overtime_never_crosses_blocked_slot(self):
        s = sched(slots={("A", 4): "blocked", ("A", 6): ("booked", "Sam K.")})
        self.assertEqual(core.compute_session_end(self.block_a(), s, 2), at("08:15"))

    def test_no_schedule_means_own_slot_only(self):
        self.assertEqual(core.compute_session_end(self.block_a(), None, 2), at("08:00"))

    def test_full_session_lifecycle_with_warnings(self):
        c = cache(sched(slots={("A", 3): ("booked", "Maya R."), ("A", 5): ("booked", "Sam K.")}))
        r = tick(at("08:01"), None, c)
        self.assertEqual(r.view.mode, RESERVED)
        blk = r.view.block
        st, evs = core.start_session(at("08:01"), r.state, blk, 3, "booking", "r01", "Maya R.", c["data"])
        self.assertEqual(evs[0]["type"], "session_start")
        self.assertEqual(tick(at("08:20"), st, c).view.warn, None)
        r = tick(at("08:25"), st, c)
        self.assertEqual((r.view.mode, r.view.warn), (IN_SESSION, "orange"))
        self.assertEqual(r.view.session_end, at("08:30"))
        self.assertEqual(r.view.next_up.name, "Sam K.")
        self.assertEqual(tick(at("08:29"), st, c).view.warn, "red")
        r = tick(at("08:30"), roundtrip(st), c)
        self.assertEqual(r.view.mode, RESERVED)
        self.assertEqual(r.view.reserved_name, "Sam K.")
        end_ev = [e for e in r.events if e["type"] == "session_end"][0]
        self.assertEqual((end_ev["slot"], end_ev["rosterId"], end_ev["minutes"]), (3, "r01", 29.0))
        self.assertIsNone(r.state["session"])

    def test_hard_lock_at_block_end(self):
        c = cache(sched())
        st, _ = core.start_session(at("08:40"), None, self.block_a(), 5, "walkup", "r02", "Sam K.", c["data"])
        self.assertEqual(tick(at("08:59", sec=59), st, c).view.mode, IN_SESSION)
        r = tick(at("09:00"), st, c)
        self.assertEqual(r.view.mode, LOCKED)
        self.assertFalse(r.view.walkup_allowed)
        self.assertEqual(r.events[0]["minutes"], 20.0)

    def test_crash_resume_mid_session(self):
        c = cache(sched())
        st, _ = core.start_session(at("07:50"), None, self.block_a(), 2, "walkup", "r02", "Sam K.", c["data"])
        r = tick(at("08:10"), roundtrip(st), c)
        self.assertEqual(r.view.mode, IN_SESSION)
        self.assertEqual(r.view.session["displayName"], "Sam K.")

    def test_outage_mid_session_keeps_timing_from_cache(self):
        c = cache(sched(slots={("A", 4): ("booked", "Maya R.")}))
        st, _ = core.start_session(at("07:50"), None, self.block_a(), 2, "walkup", "r02", "Sam K.", c["data"])
        # Later polls fail; the cache (fetched today) is unchanged → same end.
        r = tick(at("08:14"), st, c)
        self.assertEqual(r.view.session_end, at("08:15"))

    def test_woke_after_block_finalizes_with_capped_minutes(self):
        c = cache(sched())
        st, _ = core.start_session(at("08:30"), None, self.block_a(), 5, "walkup", "r02", "Sam K.", c["data"])
        r = tick(at("10:00"), st, c)                       # laptop slept through the end (before lunch arms)
        self.assertEqual(r.view.mode, DORMANT)
        self.assertEqual(r.events[0]["minutes"], 30.0)

    def test_im_done_early(self):
        c = cache(sched(slots={("A", 2): ("booked", "Maya R.")}))
        st, _ = core.start_session(at("07:46"), None, self.block_a(), 2, "booking", "r01", "Maya R.", c["data"])
        st, evs = core.end_session_now(at("07:52"), st)
        self.assertEqual(evs[0]["minutes"], 6.0)
        r = tick(at("07:53"), st, c)
        self.assertEqual(r.view.mode, LOCKED)              # booking now terminal → walk-up open
        self.assertTrue(r.view.walkup_allowed)


class JustInTime(unittest.TestCase):
    def setUp(self):
        self.blk = Block("A", MON, core.BLOCK_START_MIN["A"])
        st, _ = core.start_session(at("07:40"), None, self.blk, 1, "walkup", "r02", "Sam K.", sched())
        self.st = st

    def test_jit_booking_detected_late_gets_30s_grace(self):
        late = cache(sched(slots={("A", 2): ("booked", "Maya R.")}))   # phone booking for 07:45
        r = tick(at("07:44", sec=50), self.st, late)                    # detected 10 s before
        self.assertEqual(r.view.mode, IN_SESSION)
        self.assertEqual(r.view.warn, "red")
        self.assertEqual(r.view.session_end, at("07:45", sec=20))
        st = r.state
        self.assertEqual(tick(at("07:45", sec=19), st, late).view.mode, IN_SESSION)
        r = tick(at("07:45", sec=20), st, late)
        self.assertEqual(r.view.mode, RESERVED)
        r = tick(at("07:45", sec=21), r.state, late)
        self.assertEqual(r.view.no_show_at, at("07:50", sec=20))   # arrival window absorbs the grace

    def test_jit_detected_after_end_still_gets_grace(self):
        # Overtime into A2 (open); someone booked A3 for 08:00 but the kiosk only sees it at 08:00:05.
        c0 = cache(sched())
        self.assertEqual(tick(at("07:59"), self.st, c0).view.session_end, at("09:00"))
        late = cache(sched(slots={("A", 3): ("booked", "Maya R.")}))
        r = tick(at("08:00", sec=5), self.st, late)
        self.assertEqual(r.view.session_end, at("08:00", sec=35))
        self.assertEqual(r.view.warn, "red")

    def test_shrink_with_time_to_spare_has_no_grace(self):
        r = tick(at("07:42"), self.st, cache(sched(slots={("A", 3): ("booked", "Maya R.")})))
        self.assertEqual(r.view.session_end, at("08:00"))
        self.assertIsNone(r.state["session"]["graceUntil"])

    def test_cancelled_next_booking_extends_again(self):
        r = tick(at("07:42"), self.st, cache(sched(slots={("A", 2): ("booked", "Maya R.")})))
        self.assertEqual(r.view.session_end, at("07:45"))
        r = tick(at("07:43"), r.state, cache(sched()))
        self.assertEqual(r.view.session_end, at("09:00"))


class Teacher(unittest.TestCase):
    def test_teacher_unlock_during_session_books_done_then_rearm(self):
        c = cache(sched(slots={("A", 3): ("booked", "Maya R.")}))
        blk = core.active_block(at("08:01"))
        st, _ = core.start_session(at("08:01"), None, blk, 3, "booking", "r01", "Maya R.", c["data"])
        st, evs = core.enter_teacher(at("08:07"), st, blk)
        self.assertEqual([e["type"] for e in evs], ["session_end", "teacher_unlock"])
        self.assertEqual(evs[0]["minutes"], 6.0)
        r = tick(at("08:09"), st, c)
        self.assertEqual(r.view.mode, TEACHER)
        st, evs = core.rearm(at("08:10"), r.state, blk)
        self.assertEqual(evs[0]["type"], "rearm")
        r = tick(at("08:10"), st, c)                       # booking terminal → LOCKED, walk-up OK
        self.assertEqual(r.view.mode, LOCKED)
        self.assertTrue(r.view.walkup_allowed)

    def test_rearm_restarts_no_show_clock(self):
        c = cache(sched(slots={("A", 3): ("booked", "Maya R.")}))
        blk = core.active_block(at("08:00"))
        st = tick(at("08:00"), None, c).state
        st, _ = core.enter_teacher(at("08:01"), st, blk)
        st, _ = core.rearm(at("08:06"), st, blk)
        r = tick(at("08:06"), st, c)
        self.assertEqual(r.view.mode, RESERVED)
        self.assertEqual(r.events, [])
        self.assertEqual(r.view.no_show_at, at("08:11"))

    def test_teacher_mode_ends_at_next_armed_window(self):
        st, _ = core.enter_teacher(at("08:00"), None, core.active_block(at("08:00")))
        self.assertEqual(tick(at("09:04"), st, None).view.mode, TEACHER)
        self.assertEqual(tick(at("10:00"), st, None).view.mode, DORMANT)
        self.assertEqual(tick(at("12:55"), st, None).view.mode, LOCKED)


class BlockedPrecedence(unittest.TestCase):
    def test_date_level_block_dormant_all_day(self):
        c = cache(sched(day_blocked=True, slots={("A", 3): ("booked", "Maya R.")}))
        for t in ("07:30", "08:05", "13:10"):
            self.assertEqual(tick(at(t), None, c).view.mode, DORMANT)

    def test_class_level_block_dormant_only_that_block(self):
        c = cache(sched(class_blocked=("A",)))
        self.assertEqual(tick(at("08:00"), None, c).view.mode, DORMANT)
        self.assertEqual(tick(at("13:00"), None, c).view.mode, LOCKED)

    def test_blocked_but_booked_slot(self):
        # Backend already reports blocked for a blocked-but-booked slot.
        c = cache(sched(slots={("A", 3): "blocked"}))
        for t in ("08:00", "08:06", "08:14"):
            r = tick(at(t), None, c)
            self.assertEqual(r.view.mode, LOCKED)
            self.assertFalse(r.view.walkup_allowed)
            self.assertEqual(r.events, [])

    def test_session_ends_at_blocked_slot(self):
        c = cache(sched(slots={("A", 3): "blocked"}))
        blk = core.active_block(at("07:31"))
        st, _ = core.start_session(at("07:31"), None, blk, 1, "walkup", "r02", "Sam K.", c["data"])
        self.assertEqual(tick(at("07:59", sec=59), st, c).view.mode, IN_SESSION)
        r = tick(at("08:00"), st, c)
        self.assertEqual(r.view.mode, LOCKED)
        self.assertFalse(r.view.walkup_allowed)

    def test_block_appearing_mid_session_ends_session_with_grace(self):
        blk = core.active_block(at("07:31"))
        st, _ = core.start_session(at("07:31"), None, blk, 1, "walkup", "r02", "Sam K.", sched())
        r = tick(at("07:50"), st, cache(sched(day_blocked=True)))
        self.assertEqual(r.view.mode, IN_SESSION)
        self.assertEqual(r.view.session_end, at("07:50", sec=30))
        self.assertEqual(tick(at("07:50", sec=30), r.state, cache(sched(day_blocked=True))).view.mode, DORMANT)


class Health(unittest.TestCase):
    def test_badges(self):
        now = at("08:00")
        b = core.badges(test=False, offline=True, drift_sec=-75, events_failing=True, pending=3,
                        oldest_queued=now - timedelta(minutes=6), now=now)
        self.assertEqual(b, ["OFFLINE", "CLOCK OFF BY 75 s", "EVENTS FAILING — check agentKey", "LOG SYNC: 3 pending"])
        self.assertEqual(core.badges(test=False, offline=False, drift_sec=30, events_failing=False, pending=3,
                                     oldest_queued=now - timedelta(minutes=4), now=now), [])

    def test_offline_after_three_failures(self):
        self.assertFalse(core.is_offline(2, True))
        self.assertTrue(core.is_offline(3, True))

    def test_retry_policy(self):
        self.assertEqual(core.retry_delay_sec("ok", 0), 0)
        self.assertEqual(core.retry_delay_sec("rejected", 1), 3600)
        self.assertEqual([core.retry_delay_sec("unreachable", n) for n in (1, 2, 3, 10)], [5, 10, 20, 300])

    def test_drift(self):
        self.assertEqual(core.clock_drift_sec(1000.0, 940_000), 60.0)
        self.assertIsNone(core.clock_drift_sec(1000.0, None))


if __name__ == "__main__":
    unittest.main()
