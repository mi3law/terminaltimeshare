# AI Terminal Timeshare — v1 Specification

Booking and automatic timekeeping for one shared AI-coding workstation ("the AI terminal") in a high-school classroom. Two class blocks daily, 15-minute slots, teacher-supervised.

**Design goals:** minimal, zero-cost, casual-proof (not tamper-proof), teacher-operable from a Google Sheet.

*Rev 1.5 — class code is a switch, off by default (see §12). Rev 1.3 — school week is Sunday–Thursday (see §12). Rev 1.2 — incorporates a seven-lens adversarial design review (40 findings folded in) plus a requirements-coverage audit (all original requirements and agreed decisions verified covered) and a final author consistency pass.*

## 1. Architecture — three components

| Component | Tech | Lives | Job |
|---|---|---|---|
| Booking page (SPA) | Vanilla HTML/CSS/JS | GitHub Pages, public repo `terminaltimeshare` | Week grid, book, cancel |
| Backend | Google Apps Script web app + Google Sheet | Teacher's Google account | Store bookings, enforce rules, send email, serve JSON, admin |
| Kiosk agent | Python 3 + tkinter, stdlib only | Classroom Windows laptop (teacher's personal machine) | Timer, warnings, lock overlay, session logging |

The Sheet **is** the admin console: roster, force-cancels, blocked days, config, usage log. No admin UI gets built.

## 2. Fixed parameters

- **Block A (morning):** 7:30–9:00. Slots A1–A6 start 7:30, 7:45, 8:00, 8:15, 8:30, 8:45.
- **Block B (afternoon):** 1:00–2:30 PM. Slots B1–B6 start 1:00, 1:15, 1:30, 1:45, 2:00, 2:15.
- 15-minute slots, **solo** (one student per slot).
- **Booking window:** always the next 5 school days (**Sunday–Thursday**) including today (if today is a school day). Blocked dates render as Blocked columns and **never extend the window**. A slot is bookable online only if its start time is in the future.
- **Max 1 booking per student per calendar date.** Statuses `booked`, `done`, and `no-show` all consume the day; only `cancelled` frees it.
- Students may only book slots in **their own class's block** (roster carries class membership).
- **Walk-ups** (claimed at the machine) are uncapped, recorded only in the Log, and neither require nor consume a booking.
- **Timezone:** school local time everywhere. Dates travel as `YYYY-MM-DD` strings plus slot index — never epoch day-math — so DST transitions can't shift a booking. (See §5 "Dates in Sheets" for how this survives Google Sheets.)

## 3. Identity and secrets

- Each student is a **rosterId + display name** ("Maya R.") client-side. **rosterId is an opaque value minted for this system** (`r01`…`r24` or short random strings) — never a school-issued student ID, never an email prefix. Full names and emails exist only in the Sheet. Teacher de-duplicates display-name collisions by hand ("Maya Ro.").
- **Class code:** one shared code (Config tab), posted in the room next to the SPA's URL, rotatable any time by editing the Sheet. Known to all students by design. **Required to book only while `requireClassCode` is ticked in Config; unticked (the default) means the booking page doesn't ask for it and the backend doesn't check it.** The code value is kept either way, so switching it back on is one tick.
- **Teacher passcode:** lives only in the agent's local `config.json` on the laptop. Unlocks/overrides the kiosk any time. Entry field is always masked (the screen may be projected).
- **agentKey:** random shared secret in Config tab + agent `config.json`; authenticates the agent's event POSTs so random internet traffic can't write to the log.
- **cancelToken:** random UUID per booking, included only in the confirmation email.
- **Repo hygiene (public repo):** the repo, README, and SPA contain no school name, teacher surname, class names, or real student data; curl tests and fixtures use fake roster entries. Ship `agent/config.example.json` with placeholders; real `config.json` is gitignored. If a real secret or roster data ever reaches git history, rotate agentKey, classCode, and passcode — history survives deletion.

## 4. Booking page (GitHub Pages SPA)

- Public URL, mobile-friendly, no sign-in to view. `const EXEC_URL` at the top of `app.js` (public by design).
- **House rules blurb** rendered above the grid, one line per rule: 1 booking/day · your own block only · arrive within 5 min or your slot releases · walk-ups free whenever the machine shows LOCKED · cancel from your confirmation email.
- **Grid:** next 5 school days × both blocks × 6 slots. Cell states: `Open` / `Booked — Maya R.` / `Mine` (via localStorage name, cosmetic only) / `Blocked` / `Past` (ended slots show no name — see §5 privacy rule). A started *current* slot whose booking reached a terminal state (no-show release, teacher force-end) renders as `Released` — claimable as a walk-up at the machine, never bookable online.
- **Booking flow:** tap an open slot → pick your name (dropdown filtered to that block's class) → enter class code (only shown while it's required) → Book → confirmation banner + "check your email." If the backend reports the email failed to send, the banner says: *"Booked, but the email didn't send — ask your teacher if you need to cancel."*
- **Cancel flow:** email link opens SPA with `?cancel=TOKEN` → shows the booking → Confirm → cancelled, slot reopens. Cancel error states rendered inline: link invalid · already cancelled · slot already started ("a no-show releases it automatically — see your teacher if needed").
- One `schedule` fetch on page load (it includes the roster, §5), then polls every 60 s **with ±10 s jitter** and on tab focus. On any non-JSON or error response, keep rendering the last good grid with its "last updated" stamp and retry next tick — never blank the page.
- **Booking error states shown inline:** slot just taken · you already have a booking that day · wrong class code · name not in this block · backend busy, try again · backend unreachable ("show your teacher this message").
- Privacy: display names only; student emails never reach the browser.

## 5. Backend (Apps Script + Google Sheet)

**Sheet tabs:**

| Tab | Columns |
|---|---|
| Roster | rosterId, fullName, displayName, email, class (A/B), active |
| Bookings | id, date, class, slot, rosterId, displayName, status (`booked/done/no-show/cancelled`), cancelToken, createdAt |
| Blocked | date, class (optional), slot (optional), reason |
| Log | eventId, timestamp, event, rosterId, date, class, slot, minutes |
| Config | classCode, agentKey, requireClassCode (checkbox) |

**Status transitions (hard rule):** `booked → done | no-show | cancelled`, nothing else. `done`, `no-show`, and `cancelled` are terminal — an event targeting a row in a terminal state appends to the Log but never rewrites status. A `booked` row left behind on a past date is inert by design (the 1/day rule is keyed to the date; the Log is the fairness record).

**Dates in Sheets (correctness-critical):** Google Sheets auto-converts typed or appended `YYYY-MM-DD` strings into Date cells, which breaks string comparison. Therefore: every date column (Bookings.date, Blocked.date, Log.date) is formatted **Plain text** by setup(); the backend validates `^\d{4}-\d{2}-\d{2}$` on write, and on read normalizes any value (Date object or string) to `YYYY-MM-DD` using the spreadsheet timezone; rows that still fail validation are **skipped, never thrown on**. Both the Apps Script project timezone and the Sheet timezone are set to the school's IANA zone. The "Block a date…" menu writes canonical strings so hand-typing is the exception.

**Endpoints** (one web app, routed by `action`):

| Call | Action | Returns / does |
|---|---|---|
| GET | `schedule` | `{serverTime, classCodeRequired, roster:[{rosterId, displayName, class}], days:[{date, blocked, classes:[{class, blocked, slots:[{i, start, status, displayName?}]}]}]}`. **Privacy rule:** slots whose end time is before serverTime return `status: "past"` with no displayName (the Sheet keeps the real status); the current in-progress slot keeps its real status and name. |
| POST | `book` | Validates under the script lock: active rosterId · class code (only if `requireClassCode`) · student's class matches block · date in window + school day · **no Blocked row matches at any granularity (date, date+class, or date+class+slot)** · slot start in future · slot free · no booking that date for that student → writes row, then sends email **after releasing the lock** |
| POST | `cancel` | Token lookup; row must be `booked` **and the slot's start time must be in the future** → marks cancelled, sends cancel-confirmation email |
| POST | `events` | Requires agentKey. Body: `{agentKey, events:[{eventId, type, date, class, slot, rosterId?, minutes?, ts}]}`. Idempotent by eventId: an eventId already in the Log is silently acked. Appends Log rows; resolves (date, class, slot) to the booking row and applies the status transition table (`session_end → done`, `no_show → no-show`); walk-up events write to the Log only |

- **Locking:** `book`/`cancel`/`events` take the script lock with `waitLock(10 s)`; on failure they return a JSON "busy, try again" error (never Google's HTML error page). `SpreadsheetApp.flush()` runs **before** the lock is released; emails are sent after release. Writes re-locate rows **by booking id at write time inside the lock** — never by remembered row index — so teacher edits can't misdirect them.
- All POSTs sent as `text/plain` with a JSON body (avoids CORS preflight — Apps Script can't answer OPTIONS; responses ride the 302 to script.googleusercontent.com, which is anonymously readable only with an access-anyone deployment). Deployment: execute-as-me, access: **anyone**; **one deployment updated in place** so the `/exec` URL never changes.
- **Email** (MailApp): booking confirmation (date, time, block, "arrive within 5 minutes or your slot releases," cancel link) and cancellation confirmation. If MailApp throws after the row is written, `book` still returns success with `emailSent: false` (SPA shows the banner in §4). Quota: 1,500 recipients/day on Workspace, 100/day on a personal account — both far above the ~25/day ceiling here.
- **Teacher convenience menu** (`onOpen`): "Cancel selected booking + email student" · "Block a date…" (writes the Blocked row, then lists that date's non-cancelled bookings and offers cancel-all + email in one confirmation) · "Cancel all bookings for a date…" (optional rosterId / created-after filter, emails each affected student — the cleanup tool if the class code leaks).
- Load: ~26 steady-state requests/min is concurrency ≈1–2 against the 30-simultaneous-executions cap; the SPA-side jitter and single-fetch rule in §4 keep the 7:30 phone rush under it.

## 6. Kiosk agent (Windows laptop)

Python 3.11+ (python.org installer, tcl/tk component checked), stdlib only (`tkinter`, `urllib`, `threading`, `ctypes`, `json`, `winsound`). Code lives in the repo under `/agent`; **installed clone lives at a non-OneDrive path (e.g. `C:\kiosk`)** — OneDrive-synced folders can lock session/outbox files mid-write.

**Files** (all resolved relative to `Path(__file__).parent`, never CWD — Startup shortcuts launch with CWD=%USERPROFILE%): `config.json` `{execUrl, agentKey, passcode, pollSec: 30, testWindows?: [...]}` (hand-copied from `config.example.json`, gitignored) · `cache.json` (last schedule+roster plus its **fetch date**) · `session.json` · `outbox.json` · `agent.log`.

**Process model**
- The Startup-folder shortcut runs `watchdog.cmd`: an infinite loop that launches the agent under `pythonw` and relaunches it 5 s after any exit — a dead agent must fail **closed**, not open. The shortcut's Run mode is **Minimized** so no console window sits on the teacher's desktop. The agent exits with a **distinct exit code when the single-instance port is already taken, and the watchdog stops looping on that code** — a second launch signals the running copy and dies quietly instead of spawn-looping. All exceptions (including `root.report_callback_exception`) go to `agent.log`.
- **Single-instance guard:** the agent binds a localhost port at startup. A second launch (e.g. the teacher re-running the shortcut) detects the running copy, signals it over that port to show the **status window**, and exits — overlays never stack.
- **Status window** (small, normal window; how the teacher interacts outside class): Status · Arm now (passcode) · Quit. "Dormant" = the Tk root is `withdraw()`n — truly invisible on the teacher's personal desktop. There is no system-tray icon (not buildable in stdlib tkinter); re-running the Startup shortcut is the "reopen" gesture.
- All network I/O runs on a background worker thread with a ~10 s timeout, results handed to the UI via `root.after` — the countdown, passcode field, and walk-up button never freeze during outages. Chimes use `winsound.PlaySound(..., SND_ASYNC)`.
- At startup: `SetProcessDpiAwareness(1)` (crisp at 125–150 % scaling). During armed windows the agent holds `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED` so neither idle sleep nor display blanking fights the kiosk.

**Lifecycle**
- **Armed windows:** 7:25–9:05 and 12:55–2:35 PM on school days (Sun–Thu) whose date (or date+class, for that block) isn't blocked — or `testWindows` from config, which display a visible TEST badge. Outside armed windows the agent is dormant. Teacher mode ends automatically when the next armed window begins (fresh arm).
- Polls `schedule` every 30 s, **immediately on arming, and immediately on detecting a wake/clock jump**; caches to disk with its fetch date. After 3 failed polls: OFFLINE badge, keep running from cache. The passcode always works offline.
- **Cache validity:** the cached schedule and `session.json` are valid only while their date equals today's local date. Stale or absent cache while offline (the first-morning-of-the-week case) → the kiosk arms LOCKED with the OFFLINE badge, offers walk-ups from the cached roster, and **never enters RESERVED or posts no-show** — bookings are unknowable.
- Every UI tick is **stateless**: display = f(cached schedule, local clock, session.json). Crash or reboot mid-slot resumes the session. Clock-drift warning if local clock differs from `serverTime` by >60 s.

**States**

| State | Trigger | Screen |
|---|---|---|
| LOCKED | Armed, current slot unbooked/unclaimed/released | Fullscreen topmost overlay: clock, today's block schedule, who's next, house rules (small), walk-up button, masked passcode field |
| RESERVED | Current slot booked, not yet started | "Reserved for Maya R." + **Start my session**. **No-show rule:** fires at `max(slot start, first time this kiosk displayed RESERVED for this booking) + 5:00` — the kiosk never punishes time it wasn't armed or displayed — and only while the slot is still current; if the deadline would land past slot end, no no-show is posted (the row stays `booked`, inert). On firing: POST `no_show`, slot releases to LOCKED |
| WALK-UP claim | From LOCKED during an unbooked/released slot | Tapping the walk-up button first triggers an **immediate fresh schedule fetch**; if the slot turns out just-booked, the kiosk switches to RESERVED for the booker instead (if offline, the walk-up proceeds — accepted risk). Otherwise: pick your name (current block's roster, from cache) → session starts, Log-only |
| IN_SESSION | Started booking or walk-up | Overlay shrinks to a topmost pill: name, countdown, next-up. **End = start of the next booked-or-blocked slot, else block end** (overtime over empty slots; overtime never crosses a blocked slot). Warnings: T-5 orange + chime, T-1 red + chime. A just-in-time next booking (detected ≤30 s) shortens the session live; **if the recomputed end is ≤ now + 30 s (or past), show the red warning and lock 30 s after detection** — the incoming student's 5-minute arrival window absorbs it. At 0 → lock |
| TEACHER | Passcode entered | Fully unlocked, pill shows "Teacher mode — re-arm". **Entering teacher mode while a session is running first posts `session_end` with elapsed minutes, marking the booking `done`.** On re-arm, a current slot whose booking is in a terminal state renders LOCKED (walk-up available) for its remainder |

**Lock integrity (casual-proof)**
- The 1 s watchdog tick re-asserts topmost **and restores window state**: `deiconify()`, `lift()`, re-apply `-fullscreen` and geometry whenever the overlay is minimized, hidden, or not covering the screen — Win+D / Win+M / show-desktop self-heal within a second. Alt-F4 is swallowed.
- **Multi-display:** while LOCKED/RESERVED, if `GetSystemMetrics(SM_CMONITORS) > 1`, spawn plain black topmost `overrideredirect` toplevels covering the virtual-screen extent beyond the primary — a projector in Extend mode is not an escape hatch.
- Not blocked (accepted): Ctrl-Alt-Del / Task Manager; the Win-key Start menu can flash over the overlay (visible, self-healing).

**Blocked precedence (agent + everywhere):** blocked wins. A blocked-but-booked slot reports `blocked` in the schedule (the booking row is retained in the Sheet for the teacher to cancel); the kiosk never enters RESERVED, never posts no-show, and offers no walk-up during it; running sessions end at a blocked slot's start exactly as at a booked one. A date-level block keeps the kiosk dormant all day; a date+class block keeps it dormant for that block's armed window only.

**Event reporting**
- Events: `session_start`, `session_end` (minutes), `no_show`, `walkup_start`, `teacher_unlock`, `rearm` — each stamped with a UUID `eventId` (idempotent server-side), queued in the durable on-disk outbox, retried with backoff.
- **Failure visibility:** an auth/validation rejection (as opposed to unreachable) shows a distinct badge — "EVENTS FAILING — check agentKey" — and stops hot retries (hourly thereafter), keeping items queued. Whenever the oldest outbox entry exceeds 5 minutes, the pill/overlay shows "LOG SYNC: n pending". The Log must never die silently.

## 7. Teacher operations

- **Edit contract (prevents mid-write corruption):** in Bookings, edit cell *values* only (status, never ids) and prefer the menu for cancels; **never sort, insert, or delete rows in Bookings**. Append-only edits in Blocked; cell edits in Roster/Config are free.
- Force-end a session: walk over, passcode → teacher mode (books the session as `done` with real minutes, per §6).
- **Forgive a wrong no-show:** set the row's status to `done` (day stays consumed, mark cleared) or `cancelled` (frees the student to rebook today). **Never back to `booked`** after start+5:00 — the stateless agent would instantly re-fire the no-show.
- Blocking a date that already has bookings: the "Block a date…" menu lists them and cancels+emails in one step (§5). Blocking never silently cancels.
- Substitute / no-AI day: block the date; kiosk stays dormant.
- Class-code leak / mass fake bookings: rotate classCode in Config, then run "Cancel all bookings for a date…" per §5.
- Power settings, once: during class the laptop is plugged in with the lid open; set lid-close action to "Do nothing" on AC (lid close and critical battery override `SetThreadExecutionState`). Projector: Duplicate mode is simplest; Extend is covered by the multi-display blackout (§6).
- Usage review: Log tab is the fairness report (who used it, minutes, no-shows, walk-ups). Read it as **supervised-trust**, not verified identity — the machine takes no identity at the keyboard (v2 start-PIN is the real check).
- **Semester reset:** make a copy of the Sheet for archive, then in the **live** Sheet clear Bookings/Log data rows, replace the Roster, rotate classCode, and delete the agent's cache/outbox files. Never switch to a copied Sheet — the script and its never-changing `/exec` URL are bound to the original. (This is also the retention policy: a minor's usage/no-show log doesn't outlive the term.)
- The Sheet and the Apps Script project stay private to the teacher's account — never link-shared. Anyone needing data gets an export with Roster/Config removed. Shared access is v2.

## 8. Accepted residual risks

- A determined student can kill the overlay via Ctrl-Alt-Del / Task Manager — visible, supervised, accepted. **Doing so also exposes `config.json` (passcode + agentKey)**; both rotate cheaply (local file edit + Config tab), and the Log is supervised-trust anyway. The Win-key Start menu can flash over the overlay before the watchdog self-heals.
- **Anyone with the class code — not just classmates — can book as any student, which also sends that student a confirmation email.** The code is posted on a classroom wall (photographable). Bookings are visible on the public grid; the 1/day cap bounds the rate; rotation + "Cancel all bookings for a date…" is the recovery. Accepted for v1. **With `requireClassCode` off (the default since rev 1.5), the same applies to anyone who has the page URL**; the recovery adds "tick `requireClassCode`".
- Public endpoints expose the schedule, the active-student roster (display names, opaque ids, class letter), and booked display names — by design; no emails, no full names, and ended slots hide name + outcome (§5 privacy rule).
- Walk-up claims and "Start my session" take no identity at the machine: the Log reflects whoever was at the keyboard.
- The teacher's claude.ai account is reachable on the kiosk outside the terminal app (v2: separate browser profile / restrictions).

## 9. v2 parking lot

Google Calendar holiday sync (auto-block non-meeting days) · per-student workspace launcher (`C:\students\caps\dev\<name>`) · morning-of reminder email · 4-digit start PIN in the confirmation email (identity check at the machine) · fairness dashboard page · remote passcode management · pair bookings · shared Sheet access for a co-teacher.

## 10. Setup & deployment

1. **Platform gate (before any real code):** from the intended Google account, deploy a hello-world web app execute-as-me / access-anyone and curl it from a logged-out shell. If the school Workspace tenant blocks "anyone" access, host the Sheet + script on the teacher's personal Google account instead — same code, emails come from gmail.com, 100-recipient/day quota still ≈4× the ceiling here.
2. **Backend:** create the Sheet; run `setup()` in Apps Script to create tabs, exact column headers, and plain-text date-column formats; set script + Sheet timezone to the school's zone; fill Roster (mint `r01…` ids) and Config (classCode, random agentKey, requireClassCode unticked); deploy once; record the `/exec` URL.
3. **SPA:** set `EXEC_URL` in `app.js`; push to GitHub Pages.
4. **Agent:** clone repo to `C:\kiosk`; copy `config.example.json` → `config.json` and fill execUrl/agentKey/passcode; install Python 3.11+ (tcl/tk checked); put a shortcut to `watchdog.cmd` in the Startup folder. Update = `git pull` (config.json untouched).
5. First run inside an armed window with no cache yet: LOCKED + OFFLINE badge, walk-up-only, until the first successful poll.

## 11. Build order and acceptance tests

1. **Sheet + backend** — curl-testable before any UI exists. Must pass: a booked date round-trips as the identical `YYYY-MM-DD` string; double-book of the same slot from two parallel curls yields one success + one "slot just taken"; blocked-slot booking rejected at every granularity; cancel after slot start rejected; same eventId POSTed twice yields one Log row.
2. **SPA** on GitHub Pages.
3. **Kiosk agent.**
4. **Dress rehearsal** (under `testWindows`, TEST badge showing) before first class: book → email arrives → cancel → rebook · no-show releases at +5:00 and the Log row appears in the Sheet · walk-up claim (and walk-up button re-fetch catches a just-made booking) · overtime bumped by a just-in-time phone booking, with the ≥30 s grace · hard lock at block end · Win+D self-heals within 1 s · second monitor blacked out while LOCKED · kill the agent process — watchdog relaunches it into the same session · network cable pulled mid-session (cache keeps timing; "LOG SYNC: n pending" appears, then drains) · reboot mid-session (resumes) · teacher passcode override marks the booking `done` · blocked date keeps kiosk dormant · wrong agentKey shows "EVENTS FAILING".

## 12. Decisions log

- Stack: GH Pages SPA + Apps Script/Sheet + Python tkinter agent (chosen over all-Google, serverless, all-GitHub).
- Identity: roster dropdown + shared class code. All students have school Google Workspace email.
- Slot rules: overtime-when-free, no-show auto-release at 5 min, walk-up claiming, cancel link in email — all enabled.
- 15-min × 6 slots per block; solo only.
- Block times 7:30–9:00 and 1:00–2:30 PM; no non-meeting days handled in v1 (manual Blocked tab; calendar sync deferred to v2).
- No-shows consume the student's daily booking; walk-ups never do. Walk-ups are Log-only (no Bookings rows).
- The laptop is the teacher's personal machine — agent stays dormant outside armed windows; no IT constraints.
- Per-student workspaces deferred to v2 (students already use their own folders under `C:\students\caps\dev\`).
- Rate limits: non-issue in practice (high-tier plan, two weeks of real use).
- Rev 1.1: all seven review lenses' findings accepted and folded in (walk-up race, Sheets date coercion, Win+D recovery, watchdog fail-closed, teacher-mode status transitions, cache date-validity, blocked precedence, event idempotency + sync badges, privacy tightenings, setup/test-mode sections added).
- Rev 1.5 (owner's call): the class code is **off by default**. The roster is one class and the code was shared, not per student, so it added a step without telling students apart. It stays built in behind the Config checkbox `requireClassCode` (ticked = the page asks for it and `book` checks it; blank/unticked = neither). A blank cell counts as off, so a Sheet set up before rev 1.5 needs no edits. `schedule` reports the switch as `classCodeRequired`; a booking page talking to an older backend that doesn't send it keeps asking for the code.
- Rev 1.4 (during setup, owner's call): the booking page is branded **"CAPS AI Terminal Booking"** in the school's colours with its globe logo (matching the teacher's other school tool), relaxing the §3 "no school/class name" rule for the page's branding only. Student data, full names, emails and secrets stay out of the repo as before. Email sender name is set per-deployment via `MAIL_NAME` in the pasted `Code.gs`.
- Rev 1.3 (during setup): the school week is **Sunday–Thursday**, not Mon–Fri. "Weekday" everywhere means a Sun–Thu school day: `SCHOOL_DAYS` in `backend/Code.gs` and `SCHOOL_WEEKDAYS` in `agent/core.py` (kept in sync).
- Rev 1.2: requirements-coverage audit passed (all 15 requirements/decisions satisfied, zero holes); final consistency pass added the `Released` grid state, the minimized watchdog shortcut, and the duplicate-instance exit-code rule.
