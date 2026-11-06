"""Pure scheduling logic for the kiosk agent — no tkinter, no network, no file I/O.

Everything the screen shows is a function of (local clock, cached schedule, session state):
`tick()` takes those and returns what to display, the updated session state, and any events
to queue. Contract: SPEC.md §6 (Lifecycle, States, Blocked precedence, Event reporting).

Times are naive local datetimes (the laptop runs on school local time). The session state is
the JSON-serialisable dict persisted as session.json.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------- constants (§2, §6)

SLOT_MINUTES = 15
SLOTS_PER_BLOCK = 6
BLOCK_START_MIN = {"A": 7 * 60 + 30, "B": 13 * 60}     # A 7:30–9:00, B 1:00–2:30 PM
ARM_LEAD = timedelta(minutes=5)                        # armed 7:25 / 12:55 …
ARM_TAIL = timedelta(minutes=5)                        # … until 9:05 / 2:35
NO_SHOW_AFTER = timedelta(minutes=5)
JIT_GRACE = timedelta(seconds=30)
WARN_ORANGE_SEC = 5 * 60
WARN_RED_SEC = 60
OFFLINE_AFTER_FAILURES = 3
DRIFT_LIMIT_SEC = 60
LOG_SYNC_AGE = timedelta(minutes=5)

DORMANT, LOCKED, RESERVED, IN_SESSION, TEACHER = "DORMANT", "LOCKED", "RESERVED", "IN_SESSION", "TEACHER"

# Slot statuses (from the backend schedule) that end a running session at that slot's start.
ENDS_SESSION = {"booked", "blocked", "done", "no-show"}


# --------------------------------------------------------------------------- blocks and windows

@dataclass(frozen=True)
class Block:
    """One class block on one date. Test blocks (from config testWindows) map a real block's
    bookings (same class letter + slot numbers) onto different wall-clock times."""
    cls: str
    date: str                       # YYYY-MM-DD
    start_min: int                  # minutes after midnight of slot 1's start
    slot_minutes: int = SLOT_MINUTES
    test: bool = False

    @property
    def id(self) -> str:
        return f"{self.date}|{self.cls}|{self.start_min}|{self.slot_minutes}|{'test' if self.test else 'real'}"

    @property
    def start(self) -> datetime:
        return datetime.combine(date.fromisoformat(self.date), time()) + timedelta(minutes=self.start_min)

    def slot_start(self, i: int) -> datetime:
        return self.start + timedelta(minutes=(i - 1) * self.slot_minutes)

    def slot_end(self, i: int) -> datetime:
        return self.slot_start(i) + timedelta(minutes=self.slot_minutes)

    @property
    def end(self) -> datetime:
        return self.slot_end(SLOTS_PER_BLOCK)

    @property
    def armed_start(self) -> datetime:
        return self.start - ARM_LEAD

    @property
    def armed_end(self) -> datetime:
        return self.end + ARM_TAIL

    def is_armed_at(self, now: datetime) -> bool:
        return self.armed_start <= now < self.armed_end

    def slot_at(self, now: datetime) -> Optional[int]:
        """1-based slot whose time range contains now, or None (before/after the block)."""
        if not (self.start <= now < self.end):
            return None
        return int((now - self.start).total_seconds() // (self.slot_minutes * 60)) + 1

    def to_dict(self) -> dict:
        return {"cls": self.cls, "date": self.date, "start_min": self.start_min,
                "slot_minutes": self.slot_minutes, "test": self.test}

    @staticmethod
    def from_dict(d: dict) -> "Block":
        return Block(d["cls"], d["date"], int(d["start_min"]), int(d.get("slot_minutes", SLOT_MINUTES)),
                     bool(d.get("test", False)))


def parse_test_windows(raw: Any) -> tuple[list[dict], list[str]]:
    """Validates config testWindows: [{"block": "A"|"B", "start": "HH:MM", "date"?: "YYYY-MM-DD",
    "slotMinutes"?: int}]. Returns (valid windows, error messages); invalid entries are dropped."""
    out, errors = [], []
    if raw in (None, ""):
        return out, errors
    if not isinstance(raw, list):
        return out, ["testWindows must be a list"]
    for n, w in enumerate(raw):
        try:
            cls = str(w["block"]).upper()
            if cls not in BLOCK_START_MIN:
                raise ValueError("block must be A or B")
            hh, mm = str(w["start"]).split(":")
            start_min = int(hh) * 60 + int(mm)
            if not (0 <= start_min < 24 * 60):
                raise ValueError("start out of range")
            slot_minutes = int(w.get("slotMinutes", SLOT_MINUTES))
            if not (1 <= slot_minutes <= 60):
                raise ValueError("slotMinutes must be 1..60")
            d = w.get("date")
            if d is not None:
                date.fromisoformat(d)
            out.append({"cls": cls, "start_min": start_min, "slot_minutes": slot_minutes, "date": d})
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            errors.append(f"testWindows[{n}] ignored: {e}")
    return out, errors


def blocks_for_day(today: date, test_windows: Iterable[dict] = ()) -> list[Block]:
    """Real blocks (weekdays only) plus any test blocks for today. A test block whose armed
    window overlaps a real one is dropped — real class time always wins."""
    ds = today.isoformat()
    real = [Block(c, ds, m) for c, m in BLOCK_START_MIN.items()] if today.weekday() < 5 else []
    tests = []
    for w in test_windows:
        if w.get("date") not in (None, ds):
            continue
        b = Block(w["cls"], ds, w["start_min"], w["slot_minutes"], test=True)
        if any(b.armed_start < r.armed_end and r.armed_start < b.armed_end for r in real):
            continue
        tests.append(b)
    return real + tests


def active_block(now: datetime, test_windows: Iterable[dict] = ()) -> Optional[Block]:
    for b in blocks_for_day(now.date(), test_windows):
        if b.is_armed_at(now):
            return b
    return None


def next_armed_start(now: datetime, test_windows: Iterable[dict] = ()) -> Optional[datetime]:
    """When the kiosk next arms (today or within the next week), ignoring blocked dates."""
    for k in range(8):
        d = now.date() + timedelta(days=k)
        starts = sorted(b.armed_start for b in blocks_for_day(d, test_windows) if b.armed_start > now)
        if starts:
            return starts[0]
    return None


# --------------------------------------------------------------------------- schedule access

def schedule_for_today(cache: Optional[dict], today: str) -> Optional[dict]:
    """The cached schedule if (and only if) it was fetched today (§6 cache validity)."""
    if not cache or cache.get("fetchDate") != today:
        return None
    data = cache.get("data")
    return data if isinstance(data, dict) else None


def _day(sched: dict, d: str) -> Optional[dict]:
    for day in sched.get("days") or []:
        if day.get("date") == d:
            return day
    return None


def _class(day: Optional[dict], cls: str) -> Optional[dict]:
    if not day:
        return None
    for c in day.get("classes") or []:
        if c.get("class") == cls:
            return c
    return None


def block_is_blocked(sched: dict, d: str, cls: str) -> bool:
    """Date-level or date+class block → the kiosk stays dormant for this block (§6)."""
    day = _day(sched, d)
    c = _class(day, cls)
    return bool(day and day.get("blocked")) or bool(c and c.get("blocked"))


def slot_info(sched: Optional[dict], d: str, cls: str, i: int) -> dict:
    """{'status', 'displayName'?} for a slot; blocked wins at every level. Unknown → open."""
    if sched is None:
        return {"status": "unknown"}
    day = _day(sched, d)
    c = _class(day, cls)
    if (day and day.get("blocked")) or (c and c.get("blocked")):
        return {"status": "blocked"}
    for s in (c or {}).get("slots") or []:
        if s.get("i") == i:
            return {"status": str(s.get("status") or "open"), "displayName": s.get("displayName")}
    return {"status": "open"}


def roster_for(cache: Optional[dict], cls: str) -> list[dict]:
    """Active roster of one class from the cache — any date (walk-ups work from a stale cache)."""
    data = (cache or {}).get("data") or {}
    people = [p for p in data.get("roster") or [] if p.get("class") == cls and p.get("rosterId")]
    return sorted(people, key=lambda p: str(p.get("displayName", "")).lower())


def roster_id_for(cache: Optional[dict], cls: str, display_name: Optional[str]) -> Optional[str]:
    hits = [p["rosterId"] for p in roster_for(cache, cls) if p.get("displayName") == display_name]
    return hits[0] if len(hits) == 1 else None


def compute_session_end(block: Block, sched: Optional[dict], start_slot: int) -> datetime:
    """End = start of the next booked-or-blocked slot after the session's slot, else block end
    (overtime over empty slots, never across a blocked one). With no valid schedule the bookings
    are unknowable, so a session gets only its own slot (no overtime)."""
    if sched is None:
        return block.slot_end(start_slot)
    for j in range(start_slot + 1, SLOTS_PER_BLOCK + 1):
        if slot_info(sched, block.date, block.cls, j)["status"] in ENDS_SESSION:
            return block.slot_start(j)
    return block.end


def no_show_deadline(slot_start: datetime, first_reserved_seen: datetime) -> datetime:
    """§6: max(slot start, first time this kiosk displayed RESERVED for the booking) + 5:00."""
    return max(slot_start, first_reserved_seen) + NO_SHOW_AFTER


# --------------------------------------------------------------------------- state + events

def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def fresh_state(today: str) -> dict:
    return {"date": today, "session": None, "reservedSeen": {}, "released": [], "teacher": None}


def normalize_state(state: Any, today: str) -> dict:
    """session.json is valid only while its date equals today (§6)."""
    if not isinstance(state, dict) or state.get("date") != today:
        return fresh_state(today)
    s = copy.deepcopy(state)
    for k, v in fresh_state(today).items():
        if not isinstance(s.get(k), type(v)) and v is not None:
            s[k] = copy.deepcopy(v)
        s.setdefault(k, v)
    return s


def slot_key(block: Block, i: int) -> str:
    return f"{block.date}|{block.cls}|{i}"


def make_event(etype: str, now: datetime, block: Optional[Block], slot: Optional[int],
               roster_id: Optional[str] = None, minutes: Optional[float] = None) -> dict:
    ev = {
        "eventId": uuid.uuid4().hex,
        "type": etype,
        "date": block.date if block else now.date().isoformat(),
        "class": block.cls if block else None,
        "slot": slot,
        "ts": now.astimezone().isoformat(timespec="seconds"),
    }
    if roster_id:
        ev["rosterId"] = roster_id
    if minutes is not None:
        ev["minutes"] = minutes
    return ev


def effective_end(sess: dict) -> datetime:
    known = parse_iso(sess["knownEnd"])
    grace = parse_iso(sess.get("graceUntil"))
    return max(known, grace) if grace else known


def _update_session_end(sess: dict, block: Block, sched: Optional[dict], now: datetime) -> None:
    """Re-derive the end from the latest schedule. A shrink that leaves ≤30 s (or is already
    past) gets the just-in-time grace: lock 30 s after detection (§6 IN_SESSION)."""
    if sched is None:
        return                                  # offline later in the day: the cache keeps timing
    known = parse_iso(sess["knownEnd"])
    new = compute_session_end(block, sched, sess["slot"])
    if new < known:
        if new <= now + JIT_GRACE and not sess.get("graceUntil"):
            sess["graceUntil"] = iso(now + JIT_GRACE)
        sess["knownEnd"] = iso(new)
    elif new > known:                           # e.g. the next booking was cancelled
        sess["knownEnd"] = iso(new)
        sess["graceUntil"] = None


def _finalize_session(st: dict, end: datetime, events: list) -> None:
    sess = st["session"]
    block = Block.from_dict(sess["block"])
    started = parse_iso(sess["startedAt"])
    minutes = round(max(0.0, (end - started).total_seconds()) / 60.0, 1)
    events.append(make_event("session_end", end, block, sess["slot"], sess.get("rosterId"), minutes))
    key = slot_key(block, sess["slot"])
    if key not in st["released"]:
        st["released"].append(key)
    st["session"] = None


def start_session(now: datetime, state: dict, block: Block, slot: int, kind: str,
                  roster_id: Optional[str], display_name: str, sched: Optional[dict]) -> tuple[dict, list]:
    """kind: 'booking' (Start my session) or 'walkup'. Caller checks the view allows it."""
    st = normalize_state(state, now.date().isoformat())
    end = compute_session_end(block, sched, slot)
    st["session"] = {
        "id": uuid.uuid4().hex, "kind": kind, "block": block.to_dict(), "slot": slot,
        "rosterId": roster_id, "displayName": display_name,
        "startedAt": iso(now), "knownEnd": iso(end), "graceUntil": None,
    }
    etype = "session_start" if kind == "booking" else "walkup_start"
    return st, [make_event(etype, now, block, slot, roster_id)]


def end_session_now(now: datetime, state: dict) -> tuple[dict, list]:
    """Student taps "I'm done" (or the agent quits): book the real elapsed minutes."""
    st = normalize_state(state, now.date().isoformat())
    events: list = []
    if st["session"]:
        _finalize_session(st, min(now, effective_end(st["session"])), events)
    return st, events


def enter_teacher(now: datetime, state: dict, block: Optional[Block]) -> tuple[dict, list]:
    """Passcode accepted. A running session is ended first (booking → done, real minutes)."""
    st, events = end_session_now(now, state)
    slot = block.slot_at(now) if block else None
    st["teacher"] = {"windowId": block.id if block else None, "since": iso(now)}
    events.append(make_event("teacher_unlock", now, block, slot))
    return st, events


def rearm(now: datetime, state: dict, block: Optional[Block]) -> tuple[dict, list]:
    """Leave teacher mode. The current slot's no-show clock restarts from now: the kiosk
    never punishes time it wasn't displaying RESERVED."""
    st = normalize_state(state, now.date().isoformat())
    if not st["teacher"]:
        return st, []
    st["teacher"] = None
    slot = block.slot_at(now) if block else None
    if block and slot:
        prefix = slot_key(block, slot) + "|"
        st["reservedSeen"] = {k: v for k, v in st["reservedSeen"].items() if not k.startswith(prefix)}
    return st, [make_event("rearm", now, block, slot)]


# --------------------------------------------------------------------------- the tick

@dataclass
class SlotRow:
    i: int
    start: datetime
    status: str                 # open | booked | blocked | done | no-show | past | released | unknown
    name: Optional[str] = None


@dataclass
class View:
    mode: str
    now: datetime
    block: Optional[Block] = None
    slot: Optional[int] = None
    slot_status: Optional[str] = None
    schedule_known: bool = False
    walkup_allowed: bool = False
    reserved_name: Optional[str] = None
    no_show_at: Optional[datetime] = None      # None while RESERVED = no no-show will fire
    session: Optional[dict] = None
    session_end: Optional[datetime] = None
    remaining_sec: Optional[float] = None
    warn: Optional[str] = None                 # None | 'orange' | 'red'
    rows: list = field(default_factory=list)   # today's block schedule (SlotRow)
    next_up: Optional[SlotRow] = None
    note: str = ""                             # human-readable reason/status line

    @property
    def armed(self) -> bool:
        return self.mode != DORMANT

    @property
    def test(self) -> bool:
        return bool(self.block and self.block.test)


@dataclass
class TickResult:
    view: View
    state: dict
    events: list


def _rows(block: Block, sched: Optional[dict], st: dict, now: datetime) -> list:
    rows = []
    for i in range(1, SLOTS_PER_BLOCK + 1):
        info = slot_info(sched, block.date, block.cls, i)
        status, name = info["status"], info.get("displayName")
        if status != "blocked" and block.slot_end(i) <= now:
            status, name = "past", None                 # ended slots show no name on the kiosk either
        elif slot_key(block, i) in st["released"] and status in ("booked", "done", "no-show"):
            status, name = "released", None
        rows.append(SlotRow(i, block.slot_start(i), status, name))
    return rows


def _next_up(rows: list, after: datetime) -> Optional[SlotRow]:
    for r in rows:
        if r.start >= after and r.status in ("booked", "blocked"):
            return r
    return None


def tick(now: datetime, state: Any, cache: Optional[dict], test_windows: Iterable[dict] = ()) -> TickResult:
    """One stateless evaluation: display = f(cached schedule, local clock, session state)."""
    today = now.date().isoformat()
    st = normalize_state(state, today)
    events: list = []
    sched = schedule_for_today(cache, today)
    block = active_block(now, test_windows)
    view = View(mode=DORMANT, now=now, block=block, schedule_known=sched is not None)

    # Teacher mode belongs to one armed window; the next window is a fresh arm.
    if st["teacher"] and (block is None or st["teacher"].get("windowId") != block.id):
        st["teacher"] = None

    # A live session (survives crash/reboot via session.json).
    sess = st["session"]
    if sess:
        sblock = Block.from_dict(sess["block"])
        if block is None or sblock.id != block.id:
            _finalize_session(st, min(now, effective_end(sess)), events)
        else:
            _update_session_end(sess, sblock, sched, now)
            end = effective_end(sess)
            if now >= end:
                _finalize_session(st, end, events)
            else:
                rows = _rows(block, sched, st, now)
                remaining = (end - now).total_seconds()
                red = remaining <= WARN_RED_SEC or bool(sess.get("graceUntil"))
                view.mode = IN_SESSION
                view.slot = block.slot_at(now)
                view.session = copy.deepcopy(sess)
                view.session_end = end
                view.remaining_sec = remaining
                view.warn = "red" if red else ("orange" if remaining <= WARN_ORANGE_SEC else None)
                view.rows = rows
                view.next_up = _next_up(rows, end)
                return TickResult(view, st, events)

    if st["teacher"]:
        view.mode = TEACHER
        view.slot = block.slot_at(now)
        view.note = "Teacher mode — re-arm when done"
        return TickResult(view, st, events)

    if block is None:
        view.note = "Outside armed windows"
        return TickResult(view, st, events)

    if sched is not None and block_is_blocked(sched, block.date, block.cls):
        view.note = "Blocked today" if (_day(sched, block.date) or {}).get("blocked") else f"Block {block.cls} blocked today"
        view.block = block
        return TickResult(view, st, events)

    rows = _rows(block, sched, st, now)
    view.rows = rows
    view.mode = LOCKED
    i = block.slot_at(now)
    view.slot = i
    view.next_up = _next_up(rows, now if i is None else block.slot_end(i))

    if i is None:
        view.note = "Block starts soon" if now < block.start else "Block is over"
        return TickResult(view, st, events)

    if sched is None:
        # Stale/absent cache: bookings unknowable → never RESERVED, never no-show; walk-ups only.
        view.slot_status = "unknown"
        view.walkup_allowed = True
        view.note = "Schedule unavailable — walk-ups only"
        return TickResult(view, st, events)

    info = slot_info(sched, block.date, block.cls, i)
    status = info["status"]
    key = slot_key(block, i)
    view.slot_status = status

    if status == "blocked":
        view.note = "This slot is blocked"
        return TickResult(view, st, events)

    if status == "booked" and key not in st["released"]:
        name = info.get("displayName") or ""
        rkey = f"{key}|{name}"
        seen = parse_iso(st["reservedSeen"].get(rkey))
        if seen is None:
            seen = now
            st["reservedSeen"][rkey] = iso(now)
        deadline = no_show_deadline(block.slot_start(i), seen)
        if deadline <= block.slot_end(i) and now >= deadline:
            events.append(make_event("no_show", now, block, i, roster_id_for(cache, block.cls, name)))
            st["released"].append(key)
            view.slot_status = "released"
            view.walkup_allowed = True
            view.rows = _rows(block, sched, st, now)
            view.note = f"{name or 'Booking'} didn't arrive — slot released"
            return TickResult(view, st, events)
        view.mode = RESERVED
        view.reserved_name = name
        view.no_show_at = deadline if deadline <= block.slot_end(i) else None
        return TickResult(view, st, events)

    view.walkup_allowed = True
    view.note = "Released — open for a walk-up" if status in ("done", "no-show") or key in st["released"] else "Open — claim it as a walk-up"
    return TickResult(view, st, events)


# --------------------------------------------------------------------------- health / badges

def clock_drift_sec(local_epoch_sec: float, server_epoch_ms: Optional[float]) -> Optional[float]:
    if not isinstance(server_epoch_ms, (int, float)) or server_epoch_ms <= 0:
        return None
    return local_epoch_sec - server_epoch_ms / 1000.0


def is_offline(consecutive_failures: int, schedule_known: bool) -> bool:
    """OFFLINE after 3 failed polls, or whenever today's schedule is unknown."""
    return consecutive_failures >= OFFLINE_AFTER_FAILURES or not schedule_known


def badges(*, test: bool, offline: bool, drift_sec: Optional[float], events_failing: bool,
           pending: int, oldest_queued: Optional[datetime], now: datetime) -> list[str]:
    out = []
    if test:
        out.append("TEST")
    if offline:
        out.append("OFFLINE")
    if drift_sec is not None and abs(drift_sec) > DRIFT_LIMIT_SEC:
        out.append(f"CLOCK OFF BY {int(round(abs(drift_sec)))} s")
    if events_failing:
        out.append("EVENTS FAILING — check agentKey")
    if pending and oldest_queued is not None and now - oldest_queued > LOG_SYNC_AGE:
        out.append(f"LOG SYNC: {pending} pending")
    return out


def retry_delay_sec(outcome: str, consecutive_failures: int) -> float:
    """Outbox retry policy. 'ok' → immediately; 'rejected' (auth/validation) → hourly, no hot
    retries; anything else (unreachable, busy, non-JSON) → exponential backoff 5 s … 5 min."""
    if outcome == "ok":
        return 0.0
    if outcome == "rejected":
        return 3600.0
    return float(min(300, 5 * 2 ** max(0, consecutive_failures - 1)))
