#!/usr/bin/env python3
"""AI Terminal Timeshare — kiosk agent (Windows 11 target; runs degraded on macOS/Linux for dev).

Times sessions, warns at T-5 / T-1, locks between sessions, shows who's next, logs usage to the
backend. Contract: SPEC.md §6. All scheduling decisions live in core.py (unit-tested); this file
is the tkinter shell, the background network workers, and the Windows platform shims.

Launched by watchdog.cmd under pythonw (`pyw -3 agent.py`). Exit codes the watchdog obeys:
  3 = another copy is already running (it was asked to show its status window) → stop looping
  4 = teacher quit, or config.json is missing/invalid → stop looping
  anything else → relaunch in 5 s (a dead agent fails closed)

Dev flags: --windowed (overlay as a normal window; default off Windows), --fullscreen (force).
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import logging.handlers
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent          # never the CWD: Startup shortcuts run in %USERPROFILE%
sys.path.insert(0, str(HERE))

import core   # noqa: E402
import store  # noqa: E402

import tkinter as tk  # noqa: E402  (imported after path setup; core/store stay tkinter-free)
from tkinter import messagebox  # noqa: E402

VERSION = "1.0.0"
CONFIG_PATH = HERE / "config.json"
CACHE_PATH = HERE / "cache.json"
SESSION_PATH = HERE / "session.json"
OUTBOX_PATH = HERE / "outbox.json"
LOG_PATH = HERE / "agent.log"

EXIT_ALREADY_RUNNING = 3
EXIT_STOP = 4
INSTANCE_PORT = 47613
HTTP_TIMEOUT = 10
DORMANT_POLL_SEC = 300
WALKUP_CHECK_TIMEOUT = 12.0
WALKUP_PICK_TIMEOUT = 90.0
MAX_PASSCODE_TRIES = 5
PASSCODE_LOCKOUT_SEC = 30

IS_WIN = sys.platform == "win32"
FONT = "Segoe UI" if IS_WIN else "Helvetica"
MONO = "Consolas" if IS_WIN else "Menlo"
BG, FG, MUTED, ACCENT = "#101317", "#e8eaed", "#9aa3ae", "#14b8a6"
ORANGE, RED, BADGE = "#d97706", "#dc2626", "#f59e0b"

HOUSE_RULES = ("1 booking per day  ·  your own block only  ·  arrive within 5 min or your slot releases  ·  "
               "walk-ups are free whenever this screen shows LOCKED  ·  cancel from your confirmation email")

log = logging.getLogger("kiosk")


# =============================================================================== logging

def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    log.addHandler(fh)
    if sys.stderr is not None:                  # pythonw has no console
        log.addHandler(logging.StreamHandler(sys.stderr))

    def excepthook(t, v, tb):
        log.critical("Uncaught exception", exc_info=(t, v, tb))

    def thread_hook(args):
        log.critical("Uncaught exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


# =============================================================================== platform shims

class Platform:
    """Windows APIs behind guards. On other OSes every call is a harmless no-op / fallback."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_CMONITORS = 76, 77, 78, 79, 80

    @staticmethod
    def dpi_aware() -> None:
        if not IS_WIN:
            return
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system-aware: crisp at 125–150 %
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                log.warning("Could not set DPI awareness")

    @classmethod
    def keep_awake(cls, on: bool) -> None:
        """Hold off idle sleep and display blanking during armed windows (per-thread: UI thread)."""
        if not IS_WIN:
            return
        import ctypes
        flags = cls.ES_CONTINUOUS | (cls.ES_SYSTEM_REQUIRED | cls.ES_DISPLAY_REQUIRED if on else 0)
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            log.exception("SetThreadExecutionState failed")

    @staticmethod
    def chime(level: str, root: tk.Misc) -> None:
        if IS_WIN:
            try:
                import winsound
                alias = "SystemHand" if level == "red" else "SystemExclamation"
                winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
                return
            except Exception:
                log.exception("winsound failed")
        try:
            root.bell()
        except tk.TclError:
            pass

    @classmethod
    def extra_display_regions(cls) -> list[tuple[int, int, int, int]]:
        """Rectangles (x, y, w, h) of the virtual screen outside the primary monitor."""
        if not IS_WIN:
            return []
        import ctypes
        gsm = ctypes.windll.user32.GetSystemMetrics
        try:
            if gsm(cls.SM_CMONITORS) <= 1:
                return []
            vx, vy, vw, vh = (gsm(cls.SM_XVIRTUALSCREEN), gsm(cls.SM_YVIRTUALSCREEN),
                              gsm(cls.SM_CXVIRTUALSCREEN), gsm(cls.SM_CYVIRTUALSCREEN))
            pw, ph = gsm(cls.SM_CXSCREEN), gsm(cls.SM_CYSCREEN)
        except Exception:
            log.exception("GetSystemMetrics failed")
            return []
        regions = []
        if vx < 0:
            regions.append((vx, vy, -vx, vh))                       # left of primary
        if vx + vw > pw:
            regions.append((pw, vy, vx + vw - pw, vh))              # right of primary
        if vy < 0:
            regions.append((0, vy, pw, -vy))                        # above primary
        if vy + vh > ph:
            regions.append((0, ph, pw, vy + vh - ph))               # below primary
        return [r for r in regions if r[2] > 0 and r[3] > 0]


# =============================================================================== single instance

class SingleInstance:
    """Binds a localhost port. A second launch asks the running copy to show its status window
    and exits with EXIT_ALREADY_RUNNING — but only if a live agent actually answered, so a stuck
    port can never make the watchdog stop (fail closed)."""

    def __init__(self, ui_q: queue.Queue):
        self.ui_q = ui_q
        self.sock: Optional[socket.socket] = None

    @staticmethod
    def _ping_running() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as c:
                c.sendall(b"SHOW\n")
                c.settimeout(3)
                return c.recv(8).startswith(b"OK")
        except OSError:
            return False

    def acquire(self) -> bool:
        """True = we are the only instance. False = another live instance was signalled."""
        for attempt in range(5):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # POSIX: still one listener
            try:
                s.bind(("127.0.0.1", INSTANCE_PORT))
                s.listen(4)
                self.sock = s
                threading.Thread(target=self._serve, name="instance", daemon=True).start()
                return True
            except OSError:
                s.close()
                if self._ping_running():
                    return False
                time.sleep(1.0)
        log.warning("Instance port %s unavailable but no agent answered; running without the guard", INSTANCE_PORT)
        return True

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                if conn.recv(16).startswith(b"SHOW"):
                    conn.sendall(b"OK\n")
                    self.ui_q.put(("show_status",))
                    conn.recv(16)            # wait for the client to close first (no TIME_WAIT here)
            except OSError:
                pass
            finally:
                conn.close()


# =============================================================================== backend client

class Backend:
    def __init__(self, exec_url: str, agent_key: str):
        self.exec_url = exec_url
        self.agent_key = agent_key

    def _json(self, req: urllib.request.Request) -> dict:
        # urllib follows Apps Script's 302 (POST→GET) to script.googleusercontent.com.
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("non-object JSON")
        return data

    def schedule(self) -> dict:
        sep = "&" if "?" in self.exec_url else "?"
        return self._json(urllib.request.Request(f"{self.exec_url}{sep}action=schedule", method="GET"))

    def post_events(self, events: list) -> dict:
        body = json.dumps({"action": "events", "agentKey": self.agent_key, "events": events}).encode("utf-8")
        req = urllib.request.Request(self.exec_url, data=body, method="POST",
                                     headers={"Content-Type": "text/plain;charset=utf-8"})
        return self._json(req)


class Poller(threading.Thread):
    """Fetches the schedule every pollSec while armed (5 min while dormant), or on request."""

    def __init__(self, app: "KioskApp"):
        super().__init__(name="poller", daemon=True)
        self.app = app
        self.wake = threading.Event()
        self._seq = 0
        self._lock = threading.Lock()

    def request(self) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self.wake.set()
        return seq

    def run(self) -> None:
        while True:
            interval = self.app.poll_sec if self.app.armed else DORMANT_POLL_SEC
            self.wake.wait(timeout=interval)
            self.wake.clear()
            with self._lock:
                seq = self._seq
            t0 = time.time()
            try:
                data = self.app.backend.schedule()
                ok = data.get("ok") is True and isinstance(data.get("days"), list)
                err = None if ok else str(data.get("error"))
            except Exception as e:               # unreachable, timeout, HTML error page, …
                data, ok, err = None, False, repr(e)
            self.app.ui_q.put(("schedule", seq, ok, data, err, t0, time.time()))


class Sender(threading.Thread):
    """Drains the durable outbox. Auth/validation rejections stop hot retries (hourly)."""

    def __init__(self, app: "KioskApp"):
        super().__init__(name="sender", daemon=True)
        self.app = app
        self.wake = threading.Event()

    def run(self) -> None:
        failures, next_at, failing = 0, 0.0, False
        while True:
            self.wake.wait(timeout=1.0)
            self.wake.clear()
            if time.time() < next_at:
                continue
            batch = self.app.outbox.batch(50)
            if not batch:
                continue
            outcome = "unreachable"
            try:
                resp = self.app.backend.post_events(batch)
                if resp.get("ok") is True:
                    done = list(resp.get("acked") or []) + list(resp.get("duplicates") or [])
                    self.app.outbox.remove(done)
                    rejected = resp.get("rejected") or []
                    if rejected:
                        log.error("Backend rejected events: %s", rejected)
                    outcome = "rejected" if rejected else "ok"
                elif resp.get("error") in ("auth", "bad_request"):
                    log.error("Backend refused events: %s", resp)
                    outcome = "rejected"
                else:
                    log.warning("Event post failed: %s", resp)
            except Exception as e:
                log.warning("Event post unreachable: %r", e)
            failures = 0 if outcome == "ok" else failures + 1
            new_failing = outcome == "rejected"
            if new_failing != failing:
                failing = new_failing
                self.app.ui_q.put(("events_failing", failing))
            next_at = time.time() + core.retry_delay_sec(outcome, failures)
            if outcome == "ok" and len(self.app.outbox):
                self.wake.set()


# =============================================================================== config

class ConfigError(Exception):
    pass


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{CONFIG_PATH.name} not found.\nCopy config.example.json to config.json in\n{HERE}\nand fill it in.")
    except (OSError, ValueError) as e:
        raise ConfigError(f"{CONFIG_PATH.name} could not be read: {e}")
    if not isinstance(cfg, dict):
        raise ConfigError("config.json must be a JSON object")
    url = str(cfg.get("execUrl", ""))
    if not url.startswith("https://") or "PASTE" in url:
        raise ConfigError("config.json: execUrl must be your Apps Script https://…/exec URL")
    for k in ("agentKey", "passcode"):
        v = str(cfg.get(k, "")).strip()
        if not v or "PASTE" in v or v.startswith("CHANGE"):
            raise ConfigError(f"config.json: set a real {k}")
    try:
        cfg["pollSec"] = max(10, min(300, int(cfg.get("pollSec", 30))))
    except (TypeError, ValueError):
        cfg["pollSec"] = 30
    return cfg


# =============================================================================== formatting

def fmt_time(dt: datetime, seconds: bool = False) -> str:
    h12 = (dt.hour + 11) % 12 + 1
    s = f"{h12}:{dt.minute:02d}" + (f":{dt.second:02d}" if seconds else "")
    return s + (" AM" if dt.hour < 12 else " PM")


def fmt_left(sec: float) -> str:
    sec = max(0, int(sec + 0.999))
    return f"{sec // 60}:{sec % 60:02d}"


def row_label(r: core.SlotRow) -> str:
    return {"open": "Open", "blocked": "Blocked", "past": "—", "released": "Released", "done": "Released",
            "no-show": "Released", "unknown": "?"}.get(r.status, r.name or "Booked")


# =============================================================================== the app

class KioskApp:
    def __init__(self, root: tk.Tk, cfg: dict, ui_q: queue.Queue, windowed: bool):
        self.root = root
        self.cfg = cfg
        self.ui_q = ui_q
        self.windowed = windowed
        self.poll_sec = cfg["pollSec"]
        self.test_windows, errors = core.parse_test_windows(cfg.get("testWindows"))
        for e in errors:
            log.warning(e)
        self.backend = Backend(cfg["execUrl"], cfg["agentKey"])
        self.state = store.load_json(SESSION_PATH, None)
        self.cache = store.load_json(CACHE_PATH, None)
        self.outbox = store.Outbox(OUTBOX_PATH)

        self.armed = False
        self.view: Optional[core.View] = None
        self.failures = 0
        self.last_ok: Optional[datetime] = None
        self.drift: Optional[float] = None
        self.events_failing = False
        self.walkup: Optional[dict] = None
        self.chimed: tuple = (None, None)
        self.blackouts: list = []
        self.blackout_regions: Optional[list] = None
        self.last_wall, self.last_mono = time.time(), time.monotonic()
        self.tick_errors = 0
        self.bad_tries = 0
        self.lockout_until = 0.0
        self.done_armed_at = 0.0
        self.exit_code = 0
        self.closing = False
        self.walk_people: list = []

        self._build_overlay()
        self._build_pill()
        self._build_status()
        self.teacher_popup: Optional[tk.Toplevel] = None

        self.poller = Poller(self)
        self.sender = Sender(self)
        self.poller.start()
        self.sender.start()
        self.poller.request()                       # first poll right away
        if len(self.outbox):
            self.sender.wake.set()
        log.info("Agent %s started (windowed=%s, testWindows=%d, pending events=%d)",
                 VERSION, windowed, len(self.test_windows), len(self.outbox))
        root.after(200, self._drain)
        root.after(0, self._tick_loop)

    # ------------------------------------------------------------------ widgets

    def _build_overlay(self) -> None:
        ov = self.ov = tk.Toplevel(self.root, bg=BG)
        ov.title("AI Terminal")
        ov.withdraw()
        ov.protocol("WM_DELETE_WINDOW", lambda: None)            # Alt-F4 / close swallowed
        ov.bind("<Alt-F4>", lambda e: "break")
        inner = tk.Frame(ov, bg=BG)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        L = lambda size, **kw: tk.Label(inner, bg=BG, fg=kw.pop("fg", FG), font=(FONT, size, kw.pop("weight", "normal")), **kw)
        self.ov_badges = L(14, fg=BADGE, weight="bold")
        self.ov_badges.pack(pady=(0, 6))
        self.ov_clock = L(64, weight="bold")
        self.ov_clock.pack()
        self.ov_title = L(40, weight="bold")
        self.ov_title.pack(pady=(4, 0))
        self.ov_sub = L(18, fg=MUTED, wraplength=900, justify="center")
        self.ov_sub.pack(pady=(4, 12))

        self.ov_primary = tk.Button(inner, font=(FONT, 20, "bold"), bg=ACCENT, fg="#00110f", activebackground=ACCENT,
                                    relief="flat", padx=24, pady=10, command=self._on_primary)
        self.ov_primary_shown = False

        self.walk_frame = tk.Frame(inner, bg=BG)
        tk.Label(self.walk_frame, text="Who's using the machine?", bg=BG, fg=FG, font=(FONT, 18, "bold")).pack()
        self.walk_list = tk.Listbox(self.walk_frame, font=(FONT, 18), height=8, width=24, activestyle="none",
                                    exportselection=False)
        self.walk_list.pack(pady=6)
        self.walk_list.bind("<Double-Button-1>", lambda e: self._walkup_start())
        wb = tk.Frame(self.walk_frame, bg=BG)
        wb.pack()
        tk.Button(wb, text="Start session", font=(FONT, 16, "bold"), command=self._walkup_start).pack(side="left", padx=6)
        tk.Button(wb, text="Back", font=(FONT, 16), command=self._walkup_cancel).pack(side="left", padx=6)
        self.walk_msg = tk.Label(self.walk_frame, bg=BG, fg=BADGE, font=(FONT, 13))
        self.walk_msg.pack()
        self.walk_shown = False

        self.ov_sched = tk.Label(inner, bg=BG, fg=FG, font=(MONO, 15), justify="left")
        self.ov_sched.pack(pady=(14, 4))
        self.ov_next = L(16, weight="bold")
        self.ov_next.pack(pady=(0, 10))
        L(11, fg=MUTED, text=HOUSE_RULES, wraplength=1000, justify="center").pack(pady=(0, 14))

        pf = tk.Frame(inner, bg=BG)
        pf.pack()
        tk.Label(pf, text="Teacher passcode", bg=BG, fg=MUTED, font=(FONT, 12)).pack(side="left", padx=(0, 6))
        self.ov_pass = tk.Entry(pf, show="•", width=14, font=(FONT, 14))
        self.ov_pass.pack(side="left")
        self.ov_pass.bind("<Return>", lambda e: self._unlock_from(self.ov_pass, self.ov_pass_msg))
        tk.Button(pf, text="Unlock", font=(FONT, 12),
                  command=lambda: self._unlock_from(self.ov_pass, self.ov_pass_msg)).pack(side="left", padx=6)
        self.ov_pass_msg = tk.Label(inner, bg=BG, fg=BADGE, font=(FONT, 12))
        self.ov_pass_msg.pack(pady=(4, 0))

    def _build_pill(self) -> None:
        p = self.pill = tk.Toplevel(self.root, bg=BG)
        p.withdraw()
        p.overrideredirect(True)
        p.protocol("WM_DELETE_WINDOW", lambda: None)
        self.pill_frame = tk.Frame(p, bg=BG, padx=12, pady=8)
        self.pill_frame.pack(fill="both", expand=True)
        self.pill_name = tk.Label(self.pill_frame, bg=BG, fg=FG, font=(FONT, 12, "bold"), anchor="w")
        self.pill_name.pack(fill="x")
        self.pill_time = tk.Label(self.pill_frame, bg=BG, fg=FG, font=(FONT, 24, "bold"), anchor="w")
        self.pill_time.pack(fill="x")
        self.pill_next = tk.Label(self.pill_frame, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w")
        self.pill_next.pack(fill="x")
        self.pill_badges = tk.Label(self.pill_frame, bg=BG, fg=BADGE, font=(FONT, 9, "bold"), anchor="w")
        self.pill_badges.pack(fill="x")
        btns = self.pill_btns = tk.Frame(self.pill_frame, bg=BG)
        btns.pack(fill="x", pady=(4, 0))
        self.pill_done = tk.Button(btns, text="I'm done", font=(FONT, 10), command=self._on_done)
        self.pill_teacher = tk.Button(btns, text="Teacher", font=(FONT, 10), command=self._open_teacher_popup)
        self.pill_rearm = tk.Button(btns, text="Re-arm kiosk", font=(FONT, 11, "bold"), command=self._do_rearm)
        self.pill_btn_mode: Optional[str] = None

    def _build_status(self) -> None:
        w = self.status_win = tk.Toplevel(self.root)
        w.title("AI Terminal — kiosk agent")
        w.withdraw()
        w.protocol("WM_DELETE_WINDOW", w.withdraw)
        frm = tk.Frame(w, padx=14, pady=12)
        frm.pack(fill="both", expand=True)
        self.st_text = tk.Label(frm, font=(MONO, 11), justify="left", anchor="w")
        self.st_text.pack(fill="x")
        row = tk.Frame(frm, pady=8)
        row.pack(fill="x")
        tk.Label(row, text="Passcode").pack(side="left")
        self.st_pass = tk.Entry(row, show="•", width=14)
        self.st_pass.pack(side="left", padx=6)
        tk.Button(row, text="Arm now", command=self._status_arm).pack(side="left", padx=3)
        tk.Button(row, text="Quit agent", command=self._status_quit).pack(side="left", padx=3)
        tk.Button(row, text="Close", command=w.withdraw).pack(side="left", padx=3)
        self.st_msg = tk.Label(frm, fg="#b45309", anchor="w")
        self.st_msg.pack(fill="x")

    # ------------------------------------------------------------------ main loop

    def _tick_loop(self) -> None:
        try:
            self.tick()
            self.tick_errors = 0
        except Exception:
            self.tick_errors += 1
            log.exception("tick failed (%d in a row)", self.tick_errors)
            if self.tick_errors >= 10:
                self.quit(1)                     # watchdog relaunches us
                return
        finally:
            if not self.closing:
                delay = 1000 - int((time.time() % 1) * 1000) + 5
                self.root.after(max(50, delay), self._tick_loop)

    def tick(self) -> None:
        self._detect_clock_jump()
        res = core.tick(datetime.now(), self.state, self.cache, self.test_windows)
        self._commit(res.state, res.events)
        self._apply(res.view)

    def _commit(self, state: dict, events: list) -> None:
        if state != self.state:
            self.state = state
            try:
                store.save_json(SESSION_PATH, state)
            except OSError:
                log.exception("Could not write session.json")
        if events:
            for e in events:
                log.info("event %s %s %s%s %s", e["type"], e.get("date"), e.get("class") or "", e.get("slot") or "",
                         e.get("minutes", ""))
            try:
                self.outbox.add(events)
            except OSError:
                log.exception("Could not write outbox.json")
            self.sender.wake.set()

    def _detect_clock_jump(self) -> None:
        wall, mono = time.time(), time.monotonic()
        dw, dm = wall - self.last_wall, mono - self.last_mono
        self.last_wall, self.last_mono = wall, mono
        if dw > 10 or dw < -2 or abs(dw - dm) > 5:
            log.info("Wake / clock jump detected (wall %+.1fs, mono %+.1fs) — polling now", dw, dm)
            self.poller.request()

    # ------------------------------------------------------------------ queue from workers

    def _drain(self) -> None:
        try:
            while True:
                self._handle(self.ui_q.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            log.exception("UI message handling failed")
        finally:
            if not self.closing:
                self.root.after(200, self._drain)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "schedule":
            _, seq, ok, data, err, t0, t1 = msg
            now = datetime.now()
            if ok:
                self.cache = {"fetchDate": now.date().isoformat(), "fetchedAt": core.iso(now), "data": data}
                try:
                    store.save_json(CACHE_PATH, self.cache)
                except OSError:
                    log.exception("Could not write cache.json")
                if self.failures:
                    log.info("Schedule poll OK again after %d failures", self.failures)
                self.failures = 0
                self.last_ok = now
                self.drift = core.clock_drift_sec((t0 + t1) / 2, data.get("serverEpochMs"))
                if self.drift is not None and abs(self.drift) > core.DRIFT_LIMIT_SEC:
                    log.warning("Local clock differs from server by %.0f s", self.drift)
            else:
                self.failures += 1
                log.warning("Schedule poll failed (%d): %s", self.failures, err)
            w = self.walkup
            if w and w["stage"] == "checking" and seq >= w["seq"]:
                self.tick()                                   # re-evaluate with the fresh schedule
                self._walkup_to_picking()
        elif kind == "events_failing":
            self.events_failing = msg[1]
            log.warning("Events failing = %s", self.events_failing)
        elif kind == "show_status":
            self._show_status()

    # ------------------------------------------------------------------ rendering

    def _badges(self, v: core.View) -> list[str]:
        return core.badges(test=v.test or bool(self.test_windows), offline=core.is_offline(self.failures, v.schedule_known),
                           drift_sec=self.drift, events_failing=self.events_failing, pending=len(self.outbox),
                           oldest_queued=self.outbox.oldest_queued_at(), now=v.now)

    def _apply(self, v: core.View) -> None:
        was_armed = self.armed
        self.armed = v.armed
        self.view = v
        if self.armed != was_armed:
            Platform.keep_awake(self.armed)
            log.info("%s (%s)", "Armed" if self.armed else "Dormant", v.note or v.mode)
            if self.armed:
                self.poller.request()
        w = self.walkup
        if w and (v.mode != core.LOCKED or not v.walkup_allowed or not v.slot or
                  core.slot_key(v.block, v.slot) != w["key"]):
            self.walkup = None

        if v.mode in (core.LOCKED, core.RESERVED):
            self._hide(self.pill)
            self._close_teacher_popup()
            self._render_overlay(v)
            self._enforce_overlay()
            self._update_blackouts(True)
        elif v.mode in (core.IN_SESSION, core.TEACHER):
            self._hide(self.ov)
            self._update_blackouts(False)
            self._render_pill(v)
            self._enforce_pill()
        else:
            self._hide(self.ov)
            self._hide(self.pill)
            self._close_teacher_popup()
            self._update_blackouts(False)

        if v.mode == core.IN_SESSION and v.warn:
            sid = v.session["id"]
            if self.chimed != (sid, v.warn) and not (self.chimed[0] == sid and self.chimed[1] == "red"):
                self.chimed = (sid, v.warn)
                Platform.chime(v.warn, self.root)
        if self.status_win.state() == "normal":
            self._render_status()

    def _render_overlay(self, v: core.View) -> None:
        self.ov_badges.config(text="   ".join(self._badges(v)))
        self.ov_clock.config(text=fmt_time(v.now, seconds=True))
        blk = v.block
        slot_txt = (f"{blk.cls}{v.slot} · {fmt_time(blk.slot_start(v.slot))}–{fmt_time(blk.slot_end(v.slot))}"
                    if v.slot else f"Block {blk.cls} · {fmt_time(blk.start)}–{fmt_time(blk.end)}")
        primary = None
        if v.mode == core.RESERVED:
            self.ov_title.config(text=f"Reserved for {v.reserved_name or 'a booking'}", fg=FG)
            if v.no_show_at:
                left = (v.no_show_at - v.now).total_seconds()
                self.ov_sub.config(text=f"{slot_txt}\nTap Start within {fmt_left(left)} or the slot releases")
            else:
                self.ov_sub.config(text=f"{slot_txt}\nTap Start when you're here")
            primary = "Start my session"
        else:
            self.ov_title.config(text="LOCKED", fg=FG)
            w = self.walkup
            if w and w["stage"] == "checking":
                self.ov_sub.config(text=f"{slot_txt}\nChecking the schedule…")
            else:
                self.ov_sub.config(text=f"{slot_txt}\n{v.note}")
            if v.walkup_allowed and not w:
                primary = "Walk-up: use this slot now"

        if primary:
            self.ov_primary.config(text=primary)
            if not self.ov_primary_shown:
                self.ov_primary.pack(before=self.ov_sched, pady=6)
                self.ov_primary_shown = True
        elif self.ov_primary_shown:
            self.ov_primary.pack_forget()
            self.ov_primary_shown = False

        picking = bool(self.walkup and self.walkup["stage"] == "picking")
        if picking and not self.walk_shown:
            self.walk_frame.pack(before=self.ov_sched, pady=6)
            self.walk_shown = True
        elif not picking and self.walk_shown:
            self.walk_frame.pack_forget()
            self.walk_shown = False
        if picking and time.monotonic() - self.walkup["since"] > WALKUP_PICK_TIMEOUT:
            self._walkup_cancel()
        if self.walkup and self.walkup["stage"] == "checking" and \
                time.monotonic() - self.walkup["since"] > WALKUP_CHECK_TIMEOUT:
            log.info("Walk-up re-fetch timed out — proceeding from cache (offline)")
            self._walkup_to_picking()

        lines = []
        for r in v.rows:
            mark = "▶" if r.i == v.slot else " "
            lines.append(f"{mark} {blk.cls}{r.i}  {fmt_time(r.start):>8}  {row_label(r)}")
        self.ov_sched.config(text="\n".join(lines) if lines else "")
        nu = v.next_up
        self.ov_next.config(text=(f"Next: {fmt_time(nu.start)} — {nu.name or 'Booked'}" if nu and nu.status == "booked"
                                  else f"Next: {fmt_time(nu.start)} — Blocked" if nu
                                  else "Next: nothing booked"))
        if time.time() < self.lockout_until:
            self.ov_pass_msg.config(text=f"Too many tries — wait {int(self.lockout_until - time.time()) + 1} s")

    def _render_pill(self, v: core.View) -> None:
        if v.mode == core.TEACHER:
            color = "#1e3a8a"
            self.pill_name.config(text="Teacher mode — kiosk unlocked")
            self.pill_time.config(text=fmt_time(v.now))
            nxt = "Re-arm when done · auto re-arms at the next block"
            mode = "teacher"
        else:
            s = v.session
            color = RED if v.warn == "red" else ORANGE if v.warn == "orange" else BG
            self.pill_name.config(text=f"{s.get('displayName') or 'Walk-up'} · {'booked' if s['kind'] == 'booking' else 'walk-up'}")
            self.pill_time.config(text=f"{fmt_left(v.remaining_sec)} left")
            nu = v.next_up
            nxt = (f"Next: {fmt_time(nu.start)} {nu.name or ('Blocked' if nu.status == 'blocked' else 'Booked')}"
                   if nu else f"Free until {fmt_time(v.session_end)}")
            mode = "session"
            if self.done_armed_at and time.monotonic() - self.done_armed_at > 5:
                self.done_armed_at = 0
                self.pill_done.config(text="I'm done")
        self.pill_next.config(text=nxt)
        self.pill_badges.config(text="  ".join(self._badges(v)))
        for wdg in (self.pill, self.pill_frame, self.pill_btns, self.pill_name, self.pill_time, self.pill_next,
                    self.pill_badges):
            wdg.config(bg=color)
        if mode != self.pill_btn_mode:
            for b in (self.pill_done, self.pill_teacher, self.pill_rearm):
                b.pack_forget()
            if mode == "teacher":
                self.pill_rearm.pack(side="left")
            else:
                self.pill_done.config(text="I'm done")
                self.pill_done.pack(side="left")
                self.pill_teacher.pack(side="right")
            self.pill_btn_mode = mode

    def _render_status(self) -> None:
        v = self.view
        if v is None:
            return
        now = v.now
        if v.mode == core.DORMANT:
            nxt = core.next_armed_start(now, self.test_windows)
            mode = f"DORMANT — {v.note}" + (f"; next arm {nxt:%a} {fmt_time(nxt)}" if nxt else "")
        else:
            where = f"Block {v.block.cls}" + (f", slot {v.block.cls}{v.slot}" if v.slot else "") if v.block else ""
            mode = f"{v.mode} ({where})"
        fetched = (self.cache or {}).get("fetchedAt") or "never"
        lines = [
            f"Mode:      {mode}",
            f"Schedule:  last fetched {fetched} ({'valid today' if v.schedule_known else 'NOT valid today'})",
            f"Polls:     {'OK' if not self.failures else f'{self.failures} consecutive failures'}",
            f"Events:    {len(self.outbox)} pending{' — FAILING (check agentKey)' if self.events_failing else ''}",
            f"Clock:     {'drift unknown' if self.drift is None else f'{self.drift:+.0f} s vs server'}",
            f"Test:      {len(self.test_windows)} test window(s) configured",
            f"Badges:    {', '.join(self._badges(v)) or 'none'}",
            f"Agent:     v{VERSION} · log {LOG_PATH}",
        ]
        self.st_text.config(text="\n".join(lines))

    # ------------------------------------------------------------------ window integrity

    @staticmethod
    def _hide(w: tk.Toplevel) -> None:
        if w.state() != "withdrawn":
            w.withdraw()

    def _enforce_overlay(self) -> None:
        """Self-heal Win+D / Win+M / show-desktop within a second; stay topmost and full-screen."""
        ov = self.ov
        try:
            if ov.state() != "normal":
                ov.deiconify()
            if self.windowed:
                ov.attributes("-topmost", False)
                return
            sw, sh = ov.winfo_screenwidth(), ov.winfo_screenheight()
            if (not ov.attributes("-fullscreen") or ov.winfo_width() < sw or ov.winfo_height() < sh
                    or ov.winfo_rootx() != 0 or ov.winfo_rooty() != 0):
                ov.geometry(f"{sw}x{sh}+0+0")
                ov.attributes("-fullscreen", True)
            ov.attributes("-topmost", True)
            ov.lift()
            try:
                focused = self.root.focus_get()
            except (KeyError, tk.TclError):
                focused = None
            if focused is None:
                (self.walk_list if self.walk_shown else self.ov_pass).focus_force()
        except tk.TclError:
            log.exception("overlay enforce failed")

    def _enforce_pill(self) -> None:
        p = self.pill
        try:
            if p.state() != "normal":
                p.deiconify()
            p.update_idletasks()
            w, h = max(300, p.winfo_reqwidth()), p.winfo_reqheight()
            x = p.winfo_screenwidth() - w - 16
            geo = f"{w}x{h}+{x}+16"
            if p.geometry() != geo:
                p.geometry(geo)
            p.attributes("-topmost", True)
            p.lift()
            if self.teacher_popup is not None:
                self.teacher_popup.attributes("-topmost", True)
                self.teacher_popup.lift()
        except tk.TclError:
            log.exception("pill enforce failed")

    def _update_blackouts(self, on: bool) -> None:
        regions = Platform.extra_display_regions() if (on and not self.windowed) else []
        if regions != self.blackout_regions:
            for b in self.blackouts:
                b.destroy()
            self.blackouts = []
            for (x, y, w, h) in regions:
                t = tk.Toplevel(self.root, bg="black")
                t.overrideredirect(True)
                t.geometry(f"{w}x{h}+{x}+{y}")
                t.attributes("-topmost", True)
                t.protocol("WM_DELETE_WINDOW", lambda: None)
                self.blackouts.append(t)
            self.blackout_regions = regions
            if regions:
                log.info("Multi-display: blacking out %s", regions)
        for t in self.blackouts:
            try:
                if t.state() != "normal":
                    t.deiconify()
                t.attributes("-topmost", True)
                t.lift()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ student actions

    def _sched_today(self) -> Optional[dict]:
        return core.schedule_for_today(self.cache, datetime.now().date().isoformat())

    def _on_primary(self) -> None:
        v = self.view
        if v is None:
            return
        if v.mode == core.RESERVED:
            now = datetime.now()
            rid = core.roster_id_for(self.cache, v.block.cls, v.reserved_name)
            st, evs = core.start_session(now, self.state, v.block, v.slot, "booking", rid, v.reserved_name or "",
                                         self._sched_today())
            log.info("Booked session started: %s %s%s", v.reserved_name, v.block.cls, v.slot)
            self._commit(st, evs)
            self.tick()
        elif v.mode == core.LOCKED and v.walkup_allowed and v.slot and not self.walkup:
            # Fresh fetch first: if the slot was just booked by phone, the booker gets it instead.
            self.walkup = {"stage": "checking", "seq": self.poller.request(),
                           "key": core.slot_key(v.block, v.slot), "since": time.monotonic()}
            self._render_overlay(v)

    def _walkup_to_picking(self) -> None:
        v = self.view
        if not self.walkup or v is None or v.mode != core.LOCKED or not v.walkup_allowed:
            self.walkup = None
            return
        people = core.roster_for(self.cache, v.block.cls)
        self.walk_people = people
        self.walk_list.delete(0, "end")
        for p in people:
            self.walk_list.insert("end", p.get("displayName", "?"))
        self.walk_msg.config(text="" if people else "No roster cached yet — ask your teacher.")
        self.walkup.update(stage="picking", since=time.monotonic())
        self._render_overlay(v)
        self.walk_list.focus_set()

    def _walkup_start(self) -> None:
        v = self.view
        sel = self.walk_list.curselection()
        if not self.walkup or not sel:
            self.walk_msg.config(text="Tap your name first.")
            return
        if v is None or v.mode != core.LOCKED or not v.walkup_allowed or core.slot_key(v.block, v.slot) != self.walkup["key"]:
            self.walkup = None
            return
        p = self.walk_people[sel[0]]
        st, evs = core.start_session(datetime.now(), self.state, v.block, v.slot, "walkup", p.get("rosterId"),
                                     p.get("displayName", ""), self._sched_today())
        log.info("Walk-up started: %s %s%s", p.get("displayName"), v.block.cls, v.slot)
        self.walkup = None
        self._commit(st, evs)
        self.tick()

    def _walkup_cancel(self) -> None:
        self.walkup = None
        if self.view is not None and self.view.mode in (core.LOCKED, core.RESERVED):
            self._render_overlay(self.view)

    def _on_done(self) -> None:
        if not self.done_armed_at:
            self.done_armed_at = time.monotonic()
            self.pill_done.config(text="Tap again to end")
            return
        self.done_armed_at = 0
        st, evs = core.end_session_now(datetime.now(), self.state)
        log.info("Session ended early by student")
        self._commit(st, evs)
        self.tick()

    # ------------------------------------------------------------------ teacher actions

    def _check_passcode(self, entry: tk.Entry, msg: tk.Label) -> bool:
        typed = entry.get()
        entry.delete(0, "end")
        if time.time() < self.lockout_until:
            msg.config(text=f"Too many tries — wait {int(self.lockout_until - time.time()) + 1} s")
            return False
        if typed and hmac.compare_digest(typed.encode("utf-8"), str(self.cfg["passcode"]).encode("utf-8")):
            self.bad_tries = 0
            msg.config(text="")
            return True
        self.bad_tries += 1
        log.warning("Wrong passcode attempt (%d)", self.bad_tries)
        if self.bad_tries >= MAX_PASSCODE_TRIES:
            self.bad_tries = 0
            self.lockout_until = time.time() + PASSCODE_LOCKOUT_SEC
        msg.config(text="Wrong passcode")
        return False

    def _unlock_from(self, entry: tk.Entry, msg: tk.Label) -> None:
        if not self._check_passcode(entry, msg):
            return
        v = self.view
        st, evs = core.enter_teacher(datetime.now(), self.state, v.block if v else None)
        log.info("Teacher mode entered")
        self._close_teacher_popup()
        self._commit(st, evs)
        self.tick()

    def _open_teacher_popup(self) -> None:
        if self.teacher_popup is not None:
            return
        t = self.teacher_popup = tk.Toplevel(self.root, padx=12, pady=10)
        t.title("Teacher")
        t.attributes("-topmost", True)
        t.protocol("WM_DELETE_WINDOW", self._close_teacher_popup)
        tk.Label(t, text="Teacher passcode (ends the session)").pack()
        e = tk.Entry(t, show="•", width=16, font=(FONT, 14))
        e.pack(pady=6)
        m = tk.Label(t, fg="#b45309")
        e.bind("<Return>", lambda ev: self._unlock_from(e, m))
        row = tk.Frame(t)
        row.pack()
        tk.Button(row, text="Unlock", command=lambda: self._unlock_from(e, m)).pack(side="left", padx=4)
        tk.Button(row, text="Cancel", command=self._close_teacher_popup).pack(side="left", padx=4)
        m.pack()
        t.update_idletasks()
        t.geometry(f"+{t.winfo_screenwidth() - t.winfo_reqwidth() - 16}+{self.pill.winfo_reqheight() + 32}")
        e.focus_force()
        t.after(60000, self._close_teacher_popup)

    def _close_teacher_popup(self) -> None:
        if self.teacher_popup is not None:
            try:
                self.teacher_popup.destroy()
            except tk.TclError:
                pass
            self.teacher_popup = None

    def _do_rearm(self) -> None:
        v = self.view
        st, evs = core.rearm(datetime.now(), self.state, v.block if v else None)
        log.info("Re-armed by teacher")
        self._commit(st, evs)
        self.tick()

    def _show_status(self) -> None:
        self._render_status()
        w = self.status_win
        w.deiconify()
        w.lift()
        w.attributes("-topmost", True)
        w.after(500, lambda: w.attributes("-topmost", False))
        self.st_pass.focus_force()

    def _status_arm(self) -> None:
        if not self._check_passcode(self.st_pass, self.st_msg):
            return
        v = self.view
        if v is not None and v.mode == core.TEACHER:
            self._do_rearm()
            self.st_msg.config(text="Re-armed.")
        elif v is not None and v.armed:
            self.st_msg.config(text="Already armed.")
        else:
            nxt = core.next_armed_start(datetime.now(), self.test_windows)
            self.st_msg.config(text="Nothing to arm outside a block — arms itself" +
                               (f" {nxt:%a} {fmt_time(nxt)}." if nxt else "."))

    def _status_quit(self) -> None:
        if not self._check_passcode(self.st_pass, self.st_msg):
            return
        st, evs = core.end_session_now(datetime.now(), self.state)
        self._commit(st, evs)
        log.info("Teacher quit the agent")
        self.quit(EXIT_STOP)

    def quit(self, code: int) -> None:
        self.exit_code = code
        self.closing = True
        try:
            Platform.keep_awake(False)
        finally:
            self.root.destroy()


# =============================================================================== entry point

def main() -> int:
    ap = argparse.ArgumentParser(description="AI Terminal Timeshare kiosk agent")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--windowed", action="store_true", help="dev: overlay as a normal window")
    g.add_argument("--fullscreen", action="store_true", help="force the real full-screen overlay (default on Windows)")
    args = ap.parse_args()
    windowed = args.windowed or (not IS_WIN and not args.fullscreen)

    setup_logging()
    Platform.dpi_aware()
    ui_q: queue.Queue = queue.Queue()
    if not SingleInstance(ui_q).acquire():
        log.info("Another agent is running — asked it to show its status window; exiting")
        return EXIT_ALREADY_RUNNING

    root = tk.Tk()
    root.withdraw()                              # dormant = invisible
    root.title("AI Terminal")

    def report(exc, val, tb):
        log.error("Tk callback exception", exc_info=(exc, val, tb))
    root.report_callback_exception = report

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("Config error: %s", e)
        messagebox.showerror("AI Terminal kiosk agent", str(e))
        root.destroy()
        return EXIT_STOP

    app = KioskApp(root, cfg, ui_q, windowed)
    root.mainloop()
    log.info("Agent exiting with code %s", app.exit_code)
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
