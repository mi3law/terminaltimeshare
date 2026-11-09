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
import tkinter.font as tkfont  # noqa: E402

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
PILL_MARGIN = 16                                # default pill spot: this far in from the top-right corner

IS_WIN = sys.platform == "win32"
FONT = "Segoe UI" if IS_WIN else "Helvetica"
MONO = "Consolas" if IS_WIN else "Menlo"
# The look follows the school wallpaper: a deep navy vignette, the school-blue seal (#233e82, also the
# booking page's blue) with a soft glow and a champagne-gold ring, white words. Gold marks what you
# can act on, as on the booking page.
NAVY_EDGE, NAVY_MID, GLOW = "#050812", "#0e1631", "#1b2f63"
SCHOOL_BLUE, RING_GOLD = "#233e82", "#d0b07c"
GOLD, GOLD_HI, ON_GOLD = "#e2c48c", "#efd7a8", "#0b1633"
FG, MUTED, DIM = "#f4f6fb", "#a9b7dc", "#6f7ea8"
TILE, TILE_HI, TILE_PAST, LINE, LINE_DIM = "#111d3f", "#1b2e5e", "#0b1330", "#2b4179", "#1a2850"
WARN_BG, WARN_FG, BLOCKED_FG = "#3b2d12", "#ffe2a8", "#f3a99b"
BG, ORANGE, RED, BADGE = NAVY_MID, "#d97706", "#dc2626", WARN_FG       # in-session pill
BRAND = "CAPS AI Terminal"
NAMES_PER_PAGE = 20

# Lock-screen type: size in px at 100 % Windows scaling; the first installed family wins (the Mac
# fallbacks are only for dev previews).
_LIGHT = (("Segoe UI Light", "normal"), ("Helvetica Neue", "normal"), ("Helvetica", "normal"))
_TEXT = (("Segoe UI", "normal"), ("Helvetica Neue", "normal"), ("Helvetica", "normal"))
_SEMI = (("Segoe UI Semibold", "normal"), ("Helvetica Neue", "bold"), ("Helvetica", "bold"))
TYPE = {"clock": (84, _LIGHT), "caps": (17, _SEMI), "name": (46, _SEMI), "slot": (26, _SEMI),
        "note": (19, _TEXT), "button": (21, _SEMI), "small_btn": (15, _SEMI), "brand": (20, _SEMI),
        "badge": (13, _SEMI), "tile_code": (14, _SEMI), "tile_time": (19, _SEMI), "tile_label": (17, _TEXT),
        "next": (17, _TEXT), "name_tile": (19, _SEMI), "small": (14, _TEXT), "entry": (16, _TEXT),
        "msg": (15, _TEXT)}

HOUSE_RULES = ("1 booking per day  ·  your own block only  ·  arrive within 5 min or your slot releases  ·  "
               "walk-ups are free whenever this screen shows OPEN  ·  cancel from your confirmation email")
# The open screen's headline already says "open for walk-ups", so core's notes are reworded there.
OPEN_NOTES = {"Open — claim it as a walk-up": "Nobody has booked this slot — no teacher needed",
              "Released — open for a walk-up": "This slot was released — no teacher needed"}

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


def on_screen(x: int, y: int, w: int, h: int, sw: int, sh: int) -> tuple[int, int]:
    """Clamp a w×h window's top-left so all of it stays on an sw×sh screen."""
    return max(0, min(x, sw - w)), max(0, min(y, sh - h))


def row_label(r: core.SlotRow) -> str:
    return {"open": "Open", "blocked": "Blocked", "past": "—", "released": "Released", "done": "Released",
            "no-show": "Released", "unknown": "?"}.get(r.status, r.name or "Booked")


# =============================================================================== lock screen

def ui_scale(root: tk.Misc) -> float:
    """Design px → screen px: 1.0 at 100 % Windows display scaling, 1.25 at 125 %, …"""
    try:
        return max(0.75, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def mix(a: str, b: str, t: float) -> str:
    """The colour t of the way from a to b (both #rrggbb)."""
    ca, cb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def spaced(text: str) -> str:
    """Letter-spaced capitals, like the ring lettering on the school seal."""
    return " ".join(text.upper())


def row_kind(r: core.SlotRow) -> str:
    if r.status in ("open", "blocked", "past", "unknown"):
        return r.status
    return "released" if r.status in ("released", "done", "no-show") else "booked"


class LockScreen:
    """The LOCKED / RESERVED overlay and the walk-up name picker, drawn on one canvas.

    render(model) redraws when anything but the clock, the no-show countdown or the passcode
    message changed (those three update in place). Sizes follow the display scaling and shrink
    to fit the screen, so nothing is ever cut off."""

    DYNAMIC = ("clock", "countdown", "pass_msg")

    def __init__(self, parent: tk.Toplevel, actions: dict):
        self.actions = actions          # primary, back, start, prev, next, unlock → fn(); tile → fn(i)
        cv = self.cv = tk.Canvas(parent, bg=NAVY_EDGE, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        self.base = ui_scale(parent)
        self.scale: Optional[float] = None
        installed = set(tkfont.families(parent))
        self.fonts: dict = {}
        for role, (px, choices) in TYPE.items():
            family, weight = next((c for c in choices if c[0] in installed), choices[-1])
            self.fonts[role] = tkfont.Font(root=parent, family=family, size=-px, weight=weight)
        self.entry = tk.Entry(cv, show="•", relief="flat", bd=6, bg=TILE_PAST, fg=FG, insertbackground=FG,
                              highlightthickness=1, highlightbackground=LINE, highlightcolor=GOLD,
                              font=self.fonts["entry"])
        self.entry.bind("<Return>", lambda e: self.actions["unlock"]())
        self.entry_item = cv.create_window(0, 0, window=self.entry, anchor="e", state="hidden")
        self.logo_files: dict = {}
        for p in (HERE / "assets").glob("globe-*.png"):
            try:
                self.logo_files[int(p.stem.split("-")[1])] = p
            except ValueError:
                pass
        self.logos: dict = {}
        self.model: Optional[dict] = None
        self.static: Optional[dict] = None
        self.dyn: dict = {}
        self.size = (0, 0)
        self.colors: dict = {}          # hot tag → (fill, hover fill)
        for tag in ("btn", "tile"):
            cv.tag_bind(tag, "<Enter>", self._enter)
            cv.tag_bind(tag, "<Leave>", self._leave)
            cv.tag_bind(tag, "<Button-1>", self._click)
        cv.tag_bind("tile", "<Double-Button-1>", lambda e: self.actions["start"]())
        cv.bind("<Configure>", lambda e: self._redraw() if (e.width, e.height) != self.size else None)

    # ------------------------------------------------------------------ public

    def render(self, m: dict) -> None:
        self.model = m
        static = {k: v for k, v in m.items() if k not in self.DYNAMIC}
        static["countdown?"] = m.get("countdown") is not None
        if static != self.static:
            self.static = static
            self._redraw()
            return
        for k in self.DYNAMIC:
            if m.get(k) != self.dyn.get(k):
                self.cv.itemconfig(f"dyn:{k}", text=m.get(k) or "")
                self.dyn[k] = m.get(k)

    # ------------------------------------------------------------------ layout

    def _redraw(self) -> None:
        m, cv = self.model, self.cv
        W, H = cv.winfo_width(), cv.winfo_height()
        if m is None or W < 200 or H < 200:
            return
        if (W, H) != self.size:
            self.size = (W, H)
            self._background(W, H)
        s = self.base
        for _ in range(6):                          # shrink until the centre fits between header and footer
            self._set_scale(s)
            cv.delete("content")
            self.colors.clear()
            top, bottom = self._chrome(m, W, H, s)
            self._center(m, W, s)
            x0, y0, x1, y1 = cv.bbox("center")
            room_w, room_h = W - 64 * s, bottom - top
            if (y1 - y0 <= room_h and x1 - x0 <= room_w) or s < 0.5 * self.base:
                break
            s *= 0.98 * min(room_h / (y1 - y0), room_w / (x1 - x0))
        cv.move("center", 0, top + max(0.0, (room_h - (y1 - y0)) / 2) - y0)
        self.dyn = {k: m.get(k) for k in self.DYNAMIC}
        self._rehover()

    def _set_scale(self, s: float) -> None:
        if s != self.scale:
            self.scale = s
            for role, f in self.fonts.items():
                f.configure(size=-max(7, round(TYPE[role][0] * s)))

    def _background(self, W: int, H: int) -> None:
        """The wallpaper's vignette: lighter navy in the middle, near-black at the edges."""
        cv, cx, cy, steps = self.cv, W / 2, H * 0.45, 40
        cv.delete("bg")
        for i in range(steps):
            t = 1 - i / steps
            rx, ry = W * 0.75 * t, H * 0.8 * t
            cv.create_oval(cx - rx, cy - ry, cx + rx, cy + ry, outline="", tags="bg",
                           fill=mix(NAVY_MID, NAVY_EDGE, t ** 1.5))
        cv.tag_lower("bg")

    def _chrome(self, m: dict, W: int, H: int, s: float) -> tuple:
        """Header (globe + name, warning badges) and footer (house rules, teacher passcode).
        Returns the band (top, bottom) left for the centre."""
        cv, T, M = self.cv, ("content",), 36 * s
        logo = self._logo(round(40 * s))
        head_h = max(self._ls("brand"), logo.height() if logo else 0)
        mid, x = M + head_h / 2, M
        if logo:
            cv.create_image(x, mid, image=logo, anchor="w", tags=T)
            x += logo.width() + 14 * s
        cv.create_text(x, mid, text=BRAND, font=self.fonts["brand"], fill=FG, anchor="w", tags=T)
        x, f = W - M, self.fonts["badge"]
        for b in reversed(m["badges"]):
            w, h = f.measure(b) + 28 * s, self._ls("badge") + 12 * s
            self._rrect(x - w, mid - h / 2, x, mid + h / 2, h / 2, fill=WARN_BG,
                        outline=mix(WARN_FG, WARN_BG, 0.55), tags=T)
            cv.create_text(x - w / 2, mid, text=b, font=f, fill=WARN_FG, tags=T)
            x -= w + 10 * s

        # Teacher passcode: bottom-right, small and out of the students' way.
        fh, ew = 42 * s, 180 * s
        fy = H - M - fh / 2
        ux = self._button(W - M, fy, "Unlock", "unlock", "small", s, T, anchor="e")[0]
        cv.coords(self.entry_item, ux - 10 * s, fy)
        cv.itemconfig(self.entry_item, width=round(ew), height=round(fh), state="normal")
        lx = ux - 10 * s - ew - 12 * s
        cv.create_text(lx, fy, text="Teacher", font=self.fonts["small"], fill=DIM, anchor="e", tags=T)
        msg_y = fy - fh / 2 - 8 * s
        cv.create_text(W - M, msg_y, text=m.get("pass_msg") or "", font=self.fonts["msg"], fill=WARN_FG,
                       anchor="se", tags=T + ("dyn:pass_msg",))
        # House rules: bottom-left, wrapped between rules, never under the passcode.
        room = lx - self.fonts["small"].measure("Teacher") - M - 48 * s
        rules = self._wrap(HOUSE_RULES.split("  ·  "), "  ·  ", "small", room)
        cv.create_text(M, H - M, text=rules, font=self.fonts["small"], fill=mix(MUTED, DIM, 0.45), anchor="sw",
                       justify="left", tags=T)
        rules_top = H - M - self._ls("small") * (rules.count("\n") + 1)
        return M + head_h + 24 * s, min(rules_top, msg_y - self._ls("msg")) - 24 * s

    def _center(self, m: dict, W: int, s: float) -> None:
        T, cx, y = ("content", "center"), W / 2, 0.0
        if m["picking"]:
            y = self._text(cx, y, m["clock"], "clock", FG, T + ("dyn:clock",))
            y = self._caps(cx, y + 4 * s, "Who's using the machine?", T)
            y = self._text(cx, y + 12 * s, m["slot"], "slot", FG, T)
            y = self._text(cx, y + 6 * s, "Tap your name, then Start.", "note", MUTED, T)
            y = self._names(cx, y + 26 * s, m, s, T)
            names, sel = m["names"], m["sel"]
            if len(names) > NAMES_PER_PAGE:
                pages = -(-len(names) // NAMES_PER_PAGE)
                label = f"Page {m['page'] + 1} of {pages}"
                prev, nxt = "‹  Previous", "Next  ›"
                (pw, bh), (nw, _) = self._btn_size(prev, "small", s), self._btn_size(nxt, "small", s)
                half = self.fonts["note"].measure(label) / 2 + 18 * s
                y += 18 * s
                self._button(cx - half - pw / 2, y, prev, "prev", "small", s, T, enabled=m["page"] > 0)
                self._button(cx + half + nw / 2, y, nxt, "next", "small", s, T, enabled=m["page"] < pages - 1)
                self.cv.create_text(cx, y + bh / 2, text=label, font=self.fonts["note"], fill=MUTED, tags=T)
                y += bh
            start = f"Start as {names[sel]}" if sel is not None else "Start session"
            (bw1, bh), (bw2, _), gap = self._btn_size("Back", "secondary", s), self._btn_size(start, "primary", s), 16 * s
            x0, y = cx - (bw1 + gap + bw2) / 2, y + 28 * s
            self._button(x0 + bw1 / 2, y, "Back", "back", "secondary", s, T)
            self._button(x0 + bw1 + gap + bw2 / 2, y, start, "start", "primary", s, T, enabled=sel is not None)
            if m["pick_msg"]:
                self._text(cx, y + bh + 14 * s, m["pick_msg"], "msg", WARN_FG, T)
            return

        r, glow = 52 * s, 26 * s
        self._seal(cx, y + glow + r, r, glow, m["icon"], T)
        y = self._text(cx, y + 2 * (glow + r) + 4 * s, m["clock"], "clock", FG, T + ("dyn:clock",))
        y = self._caps(cx, y + 2 * s, m["status"], T)
        if m["name"]:
            y = self._text(cx, y + 6 * s, m["name"], "name", FG, T)
        y = self._text(cx, y + 14 * s, m["slot"], "slot", FG, T)
        if m["note"]:
            note = "\n".join(self._wrap(p.split(" "), " ", "note", 760 * s) for p in m["note"].split("\n"))
            y = self._text(cx, y + 6 * s, note, "note", MUTED, T)
        if m["countdown"] is not None:
            y = self._text(cx, y + 6 * s, m["countdown"], "note", MUTED, T + ("dyn:countdown",))
        if m["primary"]:
            y = self._button(cx, y + 26 * s, m["primary"], "primary", "primary", s, T)[3]
        if m["rows"]:
            y = self._timeline(cx, y + 40 * s, m["rows"], s, T)
            self._text(cx, y + 16 * s, m["next_up"], "next", MUTED, T)

    # ------------------------------------------------------------------ pieces

    def _seal(self, cx: float, cy: float, r: float, glow: float, icon: str, T: tuple) -> None:
        """The school seal, simplified: a school-blue disc with a soft glow and a gold ring, and a
        white padlock (locked), open padlock (open for walk-ups) or person (reserved) inside."""
        cv, s, steps = self.cv, self.scale, 16
        for i in range(steps):
            t, rr = i / steps, r + glow * (1 - i / steps)
            cv.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline="", fill=mix(NAVY_MID, GLOW, t * t), tags=T)
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=SCHOOL_BLUE, outline="", tags=T)
        ring = r - 7 * s
        cv.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, outline=RING_GOLD,
                       width=max(1, round(1.6 * s)), tags=T)
        u = r / 52
        if icon == "person":
            cv.create_oval(cx - 11 * u, cy - 28 * u, cx + 11 * u, cy - 6 * u, fill=FG, outline="", tags=T)
            cv.create_arc(cx - 24 * u, cy - u, cx + 24 * u, cy + 47 * u, start=0, extent=180, style="chord",
                          fill=FG, outline="", tags=T)
            return
        lift = 9 * u if icon == "open" else 0                        # open: shackle lifted out of its right hole
        cy += 3 * u + lift / 2                                       # padlock: shackle, body, keyhole
        top = cy - 21 * u - lift
        cv.create_arc(cx - 14 * u, top - 14 * u, cx + 14 * u, top + 14 * u, start=0, extent=180, style="arc",
                      outline=FG, width=6 * u, tags=T)
        cv.create_line(cx - 14 * u, top, cx - 14 * u, cy - 4 * u, fill=FG, width=6 * u, tags=T)
        cv.create_line(cx + 14 * u, top, cx + 14 * u, top + 5 * u if lift else cy - 4 * u, fill=FG, width=6 * u,
                       tags=T)
        self._rrect(cx - 23 * u, cy - 6 * u, cx + 23 * u, cy + 27 * u, 6 * u, fill=FG, outline="", tags=T)
        cv.create_oval(cx - 4.5 * u, cy + 3 * u, cx + 4.5 * u, cy + 12 * u, fill=SCHOOL_BLUE, outline="", tags=T)
        cv.create_rectangle(cx - 2 * u, cy + 9 * u, cx + 2 * u, cy + 19 * u, fill=SCHOOL_BLUE, outline="", tags=T)

    def _caps(self, cx: float, y: float, text: str, T: tuple) -> float:
        """Gold letter-spaced capitals between two short gold rules."""
        f, s, text = self.fonts["caps"], self.scale, spaced(text)
        ls, half = self._ls("caps"), f.measure(text) / 2
        self.cv.create_text(cx, y, text=text, font=f, fill=RING_GOLD, anchor="n", tags=T)
        for sign in (-1, 1):
            x0 = cx + sign * (half + 18 * s)
            self.cv.create_line(x0, y + ls / 2, x0 + sign * 56 * s, y + ls / 2, fill=mix(RING_GOLD, NAVY_MID, 0.5),
                                width=max(1, round(1.2 * s)), tags=T)
        return y + ls

    def _timeline(self, cx: float, y: float, rows: list, s: float, T: tuple) -> float:
        """Today's slots in this block as a row of tiles; the current one is school blue, ringed in gold."""
        cv, n = self.cv, len(rows)
        w, gap, pad = 172 * s, 12 * s, 16 * s
        h = 2 * pad + self._ls("tile_code") + self._ls("tile_time") + self._ls("tile_label") + 8 * s
        x = cx - (n * w + (n - 1) * gap) / 2
        ink = {"open": GOLD, "booked": FG, "blocked": BLOCKED_FG, "released": MUTED, "past": DIM, "unknown": MUTED}
        for code, start, label, kind, now in rows:
            past = kind == "past" and not now
            fill, edge, lw = ((SCHOOL_BLUE, RING_GOLD, max(1, round(2 * s))) if now else
                              (TILE_PAST, LINE_DIM, 1) if past else (TILE, LINE, 1))
            self._rrect(x, y, x + w, y + h, 12 * s, fill=fill, outline=edge, width=lw, tags=T)
            ty = y + pad
            cv.create_text(x + pad, ty, text=code, font=self.fonts["tile_code"], anchor="nw", tags=T,
                           fill=RING_GOLD if now else DIM if past else MUTED)
            if now:
                cv.create_text(x + w - pad, ty, text=spaced("now"), font=self.fonts["tile_code"], fill=RING_GOLD,
                               anchor="ne", tags=T)
            ty += self._ls("tile_code") + 4 * s
            cv.create_text(x + pad, ty, text=start, font=self.fonts["tile_time"], fill=DIM if past else FG,
                           anchor="nw", tags=T)
            ty += self._ls("tile_time") + 4 * s
            cv.create_text(x + pad, ty, text=self._fit(label, "tile_label", w - 2 * pad), anchor="nw", tags=T,
                           font=self.fonts["tile_label"], fill=ink.get(kind, FG))
            x += w + gap
        return y + h

    def _names(self, cx: float, y: float, m: dict, s: float, T: tuple) -> float:
        """The walk-up picker: one tile per name, up to NAMES_PER_PAGE at a time."""
        names, sel = m["names"], m["sel"]
        paged = len(names) > NAMES_PER_PAGE
        first = m["page"] * NAMES_PER_PAGE
        chunk = names[first:first + NAMES_PER_PAGE]
        if not chunk:
            return y
        rows = 4 if paged else -(-len(chunk) // 5)
        cols = 5 if paged else -(-len(chunk) // rows)
        w, h, gap = 226 * s, 58 * s, 12 * s
        for r in range(rows):                       # a paged grid keeps its height on the last page
            line = chunk[r * cols:(r + 1) * cols]
            x = cx - (len(line) * w + (len(line) - 1) * gap) / 2
            for j, name in enumerate(line):
                i, tag = first + r * cols + j, f"tile:{first + r * cols + j}"
                chosen = i == sel
                fill, hover = (GOLD, GOLD) if chosen else (TILE, TILE_HI)
                self._rrect(x, y, x + w, y + h, 12 * s, fill=fill, outline="" if chosen else LINE,
                            width=max(1, round(1.2 * s)), tags=T + ("tile", tag, "face"))
                self.cv.create_text(x + w / 2, y + h / 2, text=self._fit(name, "name_tile", w - 24 * s),
                                    font=self.fonts["name_tile"], fill=ON_GOLD if chosen else FG,
                                    tags=T + ("tile", tag))
                self.colors[tag] = (fill, hover)
                x += w + gap
            y += h + gap
        return y - gap

    def _btn_size(self, text: str, kind: str, s: float) -> tuple:
        f = self.fonts["small_btn" if kind == "small" else "button"]
        pad = {"primary": 34, "secondary": 26, "small": 18}[kind] * s
        return f.measure(text) + 2 * pad, (42 if kind == "small" else 62) * s

    def _button(self, x: float, y: float, text: str, act: str, kind: str, s: float, T: tuple,
                anchor: str = "n", enabled: bool = True) -> tuple:
        """A pill button: primary is gold, secondary/small are outlined. anchor n: (x, y) is its top
        centre; e: its right middle. Returns its bbox."""
        w, h = self._btn_size(text, kind, s)
        x1, y1 = (x - w / 2, y) if anchor == "n" else (x - w, y - h / 2)
        if not enabled:
            fill, hover, ink, edge = TILE_PAST, TILE_PAST, DIM, LINE_DIM
        elif kind == "primary":
            fill, hover, ink, edge = GOLD, GOLD_HI, ON_GOLD, ""
        else:
            fill, hover, ink, edge = TILE, TILE_HI, FG, LINE
        hot = ("btn", f"act:{act}") if enabled else ()
        self._rrect(x1, y1, x1 + w, y1 + h, h / 2, fill=fill, outline=edge, width=max(1, round(1.5 * s)),
                    tags=T + hot + ("face",))
        self.cv.create_text(x1 + w / 2, y1 + h / 2, text=text, fill=ink, tags=T + hot,
                            font=self.fonts["small_btn" if kind == "small" else "button"])
        if enabled:
            self.colors[f"act:{act}"] = (fill, hover)
        return x1, y1, x1 + w, y1 + h

    def _text(self, x: float, y: float, text: str, role: str, fill: str, T: tuple) -> float:
        """Centred text with its top at y; returns the y just below it."""
        self.cv.create_text(x, y, text=text, font=self.fonts[role], fill=fill, anchor="n", justify="center", tags=T)
        return y + self._ls(role) * (text.count("\n") + 1)

    def _rrect(self, x1: float, y1: float, x2: float, y2: float, r: float, **kw) -> int:
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r,
               x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
               x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _ls(self, role: str) -> int:
        return self.fonts[role].metrics("linespace")

    def _wrap(self, parts: list, sep: str, role: str, width: float) -> str:
        f, lines, line = self.fonts[role], [], ""
        for p in parts:
            joined = f"{line}{sep}{p}" if line else p
            if line and f.measure(joined) > width:
                lines.append(line)
                line = p
            else:
                line = joined
        return "\n".join(lines + [line])

    def _fit(self, text: str, role: str, width: float) -> str:
        f = self.fonts[role]
        if f.measure(text) <= width:
            return text
        while text and f.measure(text + "…") > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def _logo(self, px: int) -> Optional[tk.PhotoImage]:
        """The school globe at the shipped size nearest px (Tk can't resample smoothly)."""
        if not self.logo_files:
            return None
        size = min(self.logo_files, key=lambda k: abs(k - px))
        if size not in self.logos:
            try:
                self.logos[size] = tk.PhotoImage(master=self.cv, file=str(self.logo_files[size]))
            except tk.TclError:
                log.exception("Could not load %s", self.logo_files[size])
                self.logos[size] = None
        return self.logos[size]

    # ------------------------------------------------------------------ pointer

    def _hot(self, item="current") -> Optional[str]:
        for t in self.cv.gettags(item):
            if t.startswith(("act:", "tile:")):
                return t
        return None

    def _paint(self, hot: str, on: bool) -> None:
        fill = self.colors.get(hot)
        if fill:
            self.cv.itemconfig(f"face&&{hot}", fill=fill[1] if on else fill[0])

    def _enter(self, e) -> None:
        hot = self._hot()
        if hot:
            self._paint(hot, True)
            self.cv.config(cursor="hand2")

    def _leave(self, e) -> None:
        hot = self._hot()
        if hot:
            self._paint(hot, False)
        self.cv.config(cursor="")

    def _rehover(self) -> None:
        try:
            px, py = self.cv.winfo_pointerx() - self.cv.winfo_rootx(), self.cv.winfo_pointery() - self.cv.winfo_rooty()
        except tk.TclError:
            return
        for item in reversed(self.cv.find_overlapping(px, py, px, py)):
            hot = self._hot(item)
            if hot:
                self._paint(hot, True)
                return

    def _click(self, e) -> None:
        hot = self._hot()
        if hot and hot.startswith("tile:"):
            self.actions["tile"](int(hot[5:]))
        elif hot:
            self.actions[hot[4:]]()


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
        ov = self.ov = tk.Toplevel(self.root, bg=NAVY_EDGE)
        ov.title("AI Terminal")
        ov.withdraw()
        ov.protocol("WM_DELETE_WINDOW", lambda: None)            # Alt-F4 / close swallowed
        ov.bind("<Alt-F4>", lambda e: "break")
        self.ov_msg = ""                                         # passcode feedback on the lock screen
        self.lock = LockScreen(ov, {
            "primary": self._on_primary, "back": self._walkup_cancel, "start": self._walkup_start,
            "tile": self._walkup_select, "prev": lambda: self._walkup_page(-1),
            "next": lambda: self._walkup_page(1),
            "unlock": lambda: self._unlock_from(self.lock.entry, self._ov_say)})

    def _build_pill(self) -> None:
        p = self.pill = tk.Toplevel(self.root, bg=BG)
        p.withdraw()
        p.overrideredirect(True)
        p.protocol("WM_DELETE_WINDOW", lambda: None)
        self.pill_frame = tk.Frame(p, bg=BG, padx=12, pady=8, highlightthickness=1, highlightbackground=LINE)
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
        flat = dict(relief="flat", bd=0, highlightthickness=0, padx=10, pady=3, bg=TILE_HI, fg=FG,
                    activebackground=SCHOOL_BLUE, activeforeground=FG)
        gold = dict(flat, bg=GOLD, fg=ON_GOLD, activebackground=GOLD_HI, activeforeground=ON_GOLD)
        self.pill_done = tk.Button(btns, text="I'm done", font=(FONT, 10), command=self._on_done, **flat)
        self.pill_teacher = tk.Button(btns, text="Teacher", font=(FONT, 10), command=self._open_teacher_popup, **flat)
        self.pill_rearm = tk.Button(btns, text="Re-arm kiosk", font=(FONT, 11, "bold"), command=self._do_rearm, **gold)
        self.pill_btn_mode: Optional[str] = None

        # Drag anywhere except a button to move it (it has no title bar). The spot holds for the rest of
        # that session or teacher mode; the next one starts back in the top-right corner.
        p.config(cursor="fleur")                    # children inherit the move cursor…
        for b in (self.pill_done, self.pill_teacher, self.pill_rearm):
            b.config(cursor="arrow")                # …except the buttons
        p.bind("<ButtonPress-1>", self._pill_drag_start)     # a Toplevel binding fires for all its children
        p.bind("<B1-Motion>", self._pill_drag_move)
        self.pill_pos: Optional[tuple] = None       # dragged top-left; None = default top-right
        self.pill_pos_key: Optional[str] = None     # session id or "teacher" the spot belongs to
        self.pill_grab: Optional[tuple] = None      # pointer offset inside the pill while dragging

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
        blk, w = v.block, self.walkup
        slot_txt = (f"{core.slot_code(self.cache, blk.cls, v.slot)} · {fmt_time(blk.slot_start(v.slot))}–{fmt_time(blk.slot_end(v.slot))}"
                    if v.slot else f"{core.block_label(self.cache, blk.cls)} · {fmt_time(blk.start)}–{fmt_time(blk.end)}")
        picking = bool(w and w["stage"] == "picking")
        m = {"clock": fmt_time(v.now, seconds=True), "badges": self._badges(v), "slot": slot_txt,
             "pass_msg": self._pass_msg(), "picking": picking, "icon": "lock", "status": "Locked", "name": None,
             "note": None, "countdown": None, "primary": None}
        if v.mode == core.RESERVED:
            m.update(icon="person", status="Reserved for", name=v.reserved_name or "a booking",
                     primary="Start my session")
            if v.no_show_at:
                left = (v.no_show_at - v.now).total_seconds()
                m["countdown"] = f"Tap Start within {fmt_left(left)} or the slot releases"
            else:
                m["note"] = "Tap Start when you're here"
        elif v.walkup_allowed:
            # A free slot: any student in this block starts a walk-up themselves (logged), no teacher.
            m.update(icon="open", status="Open for walk-ups",
                     note="Checking the schedule…" if w and w["stage"] == "checking" else OPEN_NOTES.get(v.note, v.note))
            if not w:
                m["primary"] = "Start a walk-up session"
        else:
            # Nothing can start right now: before or after the block's slots, or a blocked slot.
            m["note"] = (f"Walk-ups and bookings start at {fmt_time(blk.start)}" if v.slot is None and v.now < blk.start
                         else v.note)
        m["rows"] = [(core.slot_code(self.cache, blk.cls, r.i), fmt_time(r.start), row_label(r), row_kind(r),
                      r.i == v.slot) for r in v.rows]
        nu = v.next_up
        m["next_up"] = "Next up  ·  " + (f"{fmt_time(nu.start)}  ·  {nu.name or 'Booked'}" if nu and nu.status == "booked"
                                         else f"{fmt_time(nu.start)}  ·  Blocked" if nu else "nothing booked")
        m.update(names=[p.get("displayName", "?") for p in self.walk_people] if picking else [],
                 sel=w.get("sel") if picking else None, page=w.get("page", 0) if picking else 0,
                 pick_msg=w.get("msg", "") if picking else "")
        self.lock.render(m)

        if picking and time.monotonic() - w["since"] > WALKUP_PICK_TIMEOUT:
            self._walkup_cancel()
        if self.walkup and self.walkup["stage"] == "checking" and \
                time.monotonic() - self.walkup["since"] > WALKUP_CHECK_TIMEOUT:
            log.info("Walk-up re-fetch timed out — proceeding from cache (offline)")
            self._walkup_to_picking()

    def _pass_msg(self) -> str:
        if time.time() < self.lockout_until:
            return f"Too many tries — wait {int(self.lockout_until - time.time()) + 1} s"
        return self.ov_msg

    def _ov_say(self, text: str) -> None:
        self.ov_msg = text
        self._rerender()

    def _rerender(self) -> None:
        if self.view is not None and self.view.mode in (core.LOCKED, core.RESERVED):
            self._render_overlay(self.view)

    def _render_pill(self, v: core.View) -> None:
        key = "teacher" if v.mode == core.TEACHER else v.session["id"]
        if key != self.pill_pos_key:                # new session or teacher mode → back to the top-right
            self.pill_pos_key, self.pill_pos, self.pill_grab = key, None, None
        if v.mode == core.TEACHER:
            color = SCHOOL_BLUE
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
            where = (core.block_label(self.cache, v.block.cls) +
                     (f", slot {core.slot_code(self.cache, v.block.cls, v.slot)}" if v.slot else "")) if v.block else ""
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
                self.lock.entry.focus_force()
        except tk.TclError:
            log.exception("overlay enforce failed")

    def _enforce_pill(self) -> None:
        p = self.pill
        try:
            if p.state() != "normal":
                p.deiconify()
            p.update_idletasks()
            w, h = max(300, p.winfo_reqwidth()), p.winfo_reqheight()
            sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
            x, y = on_screen(*(self.pill_pos or (sw - w - PILL_MARGIN, PILL_MARGIN)), w, h, sw, sh)
            geo = f"{w}x{h}+{x}+{y}"
            if p.geometry() != geo:
                p.geometry(geo)
            p.attributes("-topmost", True)
            p.lift()
            if self.teacher_popup is not None:
                self.teacher_popup.attributes("-topmost", True)
                self.teacher_popup.lift()
        except tk.TclError:
            log.exception("pill enforce failed")

    def _pill_drag_start(self, e: tk.Event) -> None:
        if isinstance(e.widget, tk.Button):         # a press on a button is a click, not a drag
            self.pill_grab = None
            return
        self.pill_grab = (e.x_root - self.pill.winfo_rootx(), e.y_root - self.pill.winfo_rooty())

    def _pill_drag_move(self, e: tk.Event) -> None:
        if self.pill_grab is None:
            return
        p = self.pill
        gx, gy = self.pill_grab
        self.pill_pos = on_screen(e.x_root - gx, e.y_root - gy, p.winfo_width(), p.winfo_height(),
                                  p.winfo_screenwidth(), p.winfo_screenheight())
        p.geometry("+%d+%d" % self.pill_pos)

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
        self.walkup.update(stage="picking", since=time.monotonic(), sel=None, page=0,
                           msg="" if people else "No roster cached yet — ask your teacher.")
        self._render_overlay(v)

    def _walkup_select(self, i: int) -> None:
        w = self.walkup
        if w and w["stage"] == "picking" and 0 <= i < len(self.walk_people):
            w.update(sel=i, msg="", since=time.monotonic())        # a tap keeps the picker open
            self._rerender()

    def _walkup_page(self, step: int) -> None:
        w = self.walkup
        if w and w["stage"] == "picking":
            last = max(0, (len(self.walk_people) - 1) // NAMES_PER_PAGE)
            w.update(page=min(last, max(0, w.get("page", 0) + step)), since=time.monotonic())
            self._rerender()

    def _walkup_start(self) -> None:
        v, w = self.view, self.walkup
        if not w or w["stage"] != "picking" or w.get("sel") is None:
            if w:
                w["msg"] = "Tap your name first."
                self._rerender()
            return
        if v is None or v.mode != core.LOCKED or not v.walkup_allowed or core.slot_key(v.block, v.slot) != w["key"]:
            self.walkup = None
            return
        p = self.walk_people[w["sel"]]
        st, evs = core.start_session(datetime.now(), self.state, v.block, v.slot, "walkup", p.get("rosterId"),
                                     p.get("displayName", ""), self._sched_today())
        log.info("Walk-up started: %s %s%s", p.get("displayName"), v.block.cls, v.slot)
        self.walkup = None
        self._commit(st, evs)
        self.tick()

    def _walkup_cancel(self) -> None:
        self.walkup = None
        self._rerender()

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

    def _check_passcode(self, entry: tk.Entry, say) -> bool:
        """say(text) shows feedback next to the field the passcode was typed into."""
        typed = entry.get()
        entry.delete(0, "end")
        if time.time() < self.lockout_until:
            say(f"Too many tries — wait {int(self.lockout_until - time.time()) + 1} s")
            return False
        if typed and hmac.compare_digest(typed.encode("utf-8"), str(self.cfg["passcode"]).encode("utf-8")):
            self.bad_tries = 0
            say("")
            return True
        self.bad_tries += 1
        log.warning("Wrong passcode attempt (%d)", self.bad_tries)
        if self.bad_tries >= MAX_PASSCODE_TRIES:
            self.bad_tries = 0
            self.lockout_until = time.time() + PASSCODE_LOCKOUT_SEC
        say("Wrong passcode")
        return False

    def _unlock_from(self, entry: tk.Entry, say) -> None:
        if not self._check_passcode(entry, say):
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
        say = lambda text: m.config(text=text)  # noqa: E731
        e.bind("<Return>", lambda ev: self._unlock_from(e, say))
        row = tk.Frame(t)
        row.pack()
        tk.Button(row, text="Unlock", command=lambda: self._unlock_from(e, say)).pack(side="left", padx=4)
        tk.Button(row, text="Cancel", command=self._close_teacher_popup).pack(side="left", padx=4)
        m.pack()
        t.update_idletasks()
        # Next to the pill, wherever it was dragged: right-aligned under it, or above it near the bottom.
        p, tw, th, sw, sh = self.pill, t.winfo_reqwidth(), t.winfo_reqheight(), t.winfo_screenwidth(), t.winfo_screenheight()
        below = p.winfo_rooty() + p.winfo_height() + PILL_MARGIN
        y = below if below + th + 2 * PILL_MARGIN <= sh else p.winfo_rooty() - th - 3 * PILL_MARGIN   # room for a title bar
        x, y = on_screen(p.winfo_rootx() + p.winfo_width() - tw, y, tw, th, sw, sh)
        t.geometry(f"+{x}+{y}")
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
        if not self._check_passcode(self.st_pass, lambda text: self.st_msg.config(text=text)):
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
        if not self._check_passcode(self.st_pass, lambda text: self.st_msg.config(text=text)):
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
