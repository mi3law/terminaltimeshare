# AI Terminal Timeshare

Booking and automatic timekeeping for one shared AI-coding workstation in a classroom: two daily class blocks, 15-minute solo slots, teacher-supervised. The full design contract is [SPEC.md](SPEC.md). This README is the setup and operations guide.

| Part | What it is | Where it lives |
|---|---|---|
| Booking page | `index.html`, `app.js`, `style.css` (plain HTML/JS) | GitHub Pages, this public repo |
| Backend | `backend/Code.gs` (Google Apps Script) + a Google Sheet | Your Google account, private |
| Kiosk agent | `agent/` (Python 3.11+, standard library only) | The classroom Windows laptop, at `C:\kiosk` |

The **Google Sheet is the admin console**: roster, bookings, blocked days, config, and the usage log. There is no separate admin page.

> **Placeholders used below.** Replace anything in `CAPS-WITH-DASHES`: `YOUR-GITHUB-USERNAME`, your web-app `/exec` URL, your class code, and your agentKey. Never commit real student data, the class code, the agentKey, or the passcode. If one ever reaches git history, rotate all three (§ [Rotating secrets](#rotating-secrets)).

---

## Contents

1. [Platform gate (do this first)](#1-platform-gate-do-this-first)
2. [Backend: Sheet and Apps Script](#2-backend-sheet-and-apps-script)
3. [Backend acceptance tests (curl)](#3-backend-acceptance-tests-curl)
4. [Booking page on GitHub Pages](#4-booking-page-on-github-pages)
5. [Kiosk laptop install](#5-kiosk-laptop-install)
6. [Dress rehearsal (before the first class)](#6-dress-rehearsal-before-the-first-class)
7. [Teacher operations](#7-teacher-operations)
8. [For developers](#8-for-developers)

---

## 1. Platform gate (do this first)

Before anything else, prove that your Google account can publish a web app that **anyone** can reach without signing in. Some school Workspace tenants block this.

1. Signed in as the Google account you plan to use, go to <https://script.google.com>, click **New project**, and replace the code with:

   ```js
   function doGet() { return ContentService.createTextOutput('hello'); }
   ```

2. Click **Deploy → New deployment**, choose the gear icon, then **Web app**. Set **Execute as: Me** and **Who has access: Anyone**. Click **Deploy**, approve the permissions, and copy the **Web app URL** (it ends in `/exec`).
3. From a terminal (Terminal on macOS, or **Git Bash** on Windows), curl it. curl carries no Google cookies, so it is automatically "logged out":

   ```bash
   curl -sL 'https://script.google.com/macros/s/PASTE-THE-ID/exec'
   ```

   - It prints `hello`: you're good. Use this account.
   - It prints an HTML sign-in page, or the deploy dialog had no **Anyone** option: your tenant blocks it. Use your **personal Google account** for the Sheet and script instead. The code is the same, emails will come from gmail.com, and the personal quota (100 emails a day) is still about 4× what this needs.

Delete the hello-world project afterwards.

---

## 2. Backend: Sheet and Apps Script

### 2.1 Create the Sheet and paste the code

1. In the chosen account, create a new Google Sheet (name it anything, e.g. "AI terminal").
2. **Extensions → Apps Script**. Delete the sample code in `Code.gs` and paste the whole of [`backend/Code.gs`](backend/Code.gs).
3. Edit the two constants at the top:
   - `SCHOOL_TZ`: your school's IANA timezone. The laptop runs on **UTC+3**, so the default is `'Asia/Riyadh'`. **Confirm it's right for your city.** Other UTC+3 zones are `Asia/Kuwait`, `Asia/Qatar`, `Asia/Bahrain`, `Asia/Baghdad`, `Europe/Istanbul` and `Africa/Nairobi`.
   - `SPA_URL`: `https://YOUR-GITHUB-USERNAME.github.io/terminaltimeshare/`. This is where the cancel links in emails point. It's predictable, so set it now even though the page goes up in step 4.
   - `MAIL_NAME` (optional): the name your emails come from and sign off with. It also appears in subject lines. Default `'AI Terminal'`.
4. Set the **script** timezone to the same zone. Open **Project Settings** (gear icon) and either pick it under *Time zone*, or tick *Show "appsscript.json" manifest file in editor*, then set `"timeZone": "Asia/Riyadh"` (your zone) in `appsscript.json`.
5. Save, select `setup` in the function dropdown, and click **Run**. Approve the permissions (Sheets, sending email). `setup()`:
   - creates the tabs **Roster, Bookings, Blocked, Log, Config** with the exact column headers,
   - formats every date column as **Plain text** (so Google Sheets can't turn `2026-11-09` into a date and break comparisons),
   - sets the Sheet's timezone to `SCHOOL_TZ` and warns you if the script timezone differs,
   - fills **Config** with a random `classCode` and `agentKey` if they're empty, and adds an unticked `requireClassCode` checkbox (students aren't asked for the code until you tick it).

   It is safe to run again; it never deletes data.
6. Reload the Sheet. A **Timeshare** menu appears (Cancel selected booking + email student · Block a date… · Cancel all bookings for a date…).

### 2.2 Fill in the Roster and Config tabs

**Roster** has one row per student:

| rosterId | fullName | displayName | email | class | active |
|---|---|---|---|---|---|
| r01 | Maya Example | Maya E. | maya.e@school.example | A | TRUE |
| r02 | Sam Sample | Sam S. | sam.s@school.example | B | TRUE |

- `rosterId` is an **opaque id you make up for this system** (`r01`…`r24`). Never use a school student ID or an email prefix.
- `displayName` is what everyone sees on the public grid. Keep it short, and de-duplicate collisions by hand ("Maya Ro." / "Maya Re.").
- `class` is `A` (the 7:30–9:00 block) or `B` (the 1:00–2:30 PM block).
- `active` is `TRUE` to show the student; set it to `FALSE` to hide them without deleting history.
- Full names and emails never leave the Sheet.

**Config** has row 2 = `classCode`, `agentKey`, `requireClassCode`. Leave `agentKey` as the random value; you'll copy it into the laptop's `config.json`.

- `requireClassCode` is **unticked by default**: the booking page doesn't ask for a code, and anyone with the page URL can book as any student on the roster. Tick it to make students type `classCode` when they book. It takes effect right away (open booking pages pick it up within about a minute), and `classCode` keeps its value while it's off.
- If you turn it on, change `classCode` to something easy to post on the wall.
- A Sheet set up before this column existed has no `requireClassCode` cell. Blank counts as off. To get the checkbox, run `setup()` again.

### 2.3 Deploy once

1. **Deploy → New deployment → Web app**, with **Execute as: Me** and **Who has access: Anyone**. Click **Deploy**.
2. Copy the **Web app URL** (`https://script.google.com/macros/s/…/exec`). This is your **EXEC URL**; you'll paste it in two places (`app.js` and the laptop's `config.json`).

> **Updating the code later:** paste the new `Code.gs`, then **Deploy → Manage deployments → ✏️ (edit) → Version: New version → Deploy**. **Never** click *New deployment* again: that makes a new URL, and the booking page and kiosk would keep calling the old one. Saving the code alone does not change what the web app runs.

---

## 3. Backend acceptance tests (curl)

Run these from Terminal (macOS) or Git Bash (Windows) **before** building anything else (SPEC §11 step 1). They use fake test students, never real ones.

### 3.0 Setup for the tests

1. In the Apps Script editor, select `addTestStudents` and **Run**. It adds three fake students using *your own* email: `rT1` Test Alpha (A), `rT2` Test Bravo (A), `rT3` Test Charlie (B).
2. In the terminal, set these variables (copy `classCode` and `agentKey` from the Config tab):

```bash
EXEC='https://script.google.com/macros/s/PASTE-YOUR-DEPLOYMENT-ID/exec'
CODE='PASTE-CLASS-CODE'
KEY='PASTE-AGENT-KEY'
post() { curl -sL -H 'Content-Type: text/plain;charset=utf-8' --data "$1" "$EXEC"; echo; }
book() { post "{\"action\":\"book\",\"rosterId\":\"$1\",\"date\":\"$2\",\"class\":\"$3\",\"slot\":$4,\"classCode\":\"$CODE\"}"; }
```

3. Look at the booking window and note the five dates:

```bash
curl -sL "$EXEC?action=schedule" | grep -o '"date":"[0-9-]*"'
```

4. Set D2–D5 to the **2nd through 5th** dates it printed. These are all after today, so every slot on them is in the future:

```bash
D2='YYYY-MM-DD'; D3='YYYY-MM-DD'; D4='YYYY-MM-DD'; D5='YYYY-MM-DD'
```

### Test 1: a booked date round-trips as the identical `YYYY-MM-DD` string

```bash
book rT1 "$D2" A 6
```

Expect `{"ok":true,…,"emailSent":true}` and a confirmation email in your inbox. Then:

```bash
curl -sL "$EXEC?action=schedule" | grep -o "\"date\":\"$D2\""
```

Expect exactly `"date":"<D2>"`. In the **Bookings** tab, the new row's `date` cell must show the identical string, **left-aligned** (text, not a date). **Format → Number** should show *Plain text*.

### Test 2: double-booking the same slot from two parallel curls

```bash
book rT1 "$D3" A 5 & book rT2 "$D3" A 5 & wait
```

Expect **one** `{"ok":true,…}` and **one** `{"ok":false,"error":"slot_taken"}`. The order varies. Bookings must gain exactly one row for D3 A5.

### Test 3: a blocked slot is rejected at every granularity

Using the Sheet menu **Timeshare → Block a date…**, add three blocks. Answer "No" when it offers to cancel bookings; there are none.

1. `D5`, class *blank* (whole day)
2. `D4`, class `B`, slot *blank* (all of Block B)
3. `D3`, class `A`, slot `2`

Then:

```bash
book rT1 "$D5" A 3    # whole day blocked      → "error":"blocked"
book rT3 "$D4" B 3    # date + class blocked   → "error":"blocked"
book rT1 "$D3" A 2    # date + class + slot    → "error":"blocked"
book rT2 "$D4" A 3    # control: Block A on D4 is open → "ok":true
```

The schedule should also report those slots as `"status":"blocked"`.

### Test 4: cancel after the slot has started is rejected

In the **Bookings** tab, type one row by hand. Use the **previous school day's** date, typed as plain text, so the slot has certainly started:

| id | date | class | slot | rosterId | displayName | status | cancelToken | createdAt |
|---|---|---|---|---|---|---|---|---|
| b-test-started | YYYY-MM-DD (yesterday) | A | 1 | rT1 | Test Alpha | booked | test-started-token | |

```bash
post '{"action":"cancel","token":"test-started-token","confirm":true}'   # → "error":"started"
```

Positive control: copy the `cancelToken` of the Test 1 booking from the Bookings tab (or click the link in its email).

```bash
TOKEN='PASTE-CANCEL-TOKEN'
post "{\"action\":\"cancel\",\"token\":\"$TOKEN\",\"confirm\":true}"   # → "ok":true (cancel email arrives)
post "{\"action\":\"cancel\",\"token\":\"$TOKEN\",\"confirm\":true}"   # → "error":"already_cancelled"
```

### Test 5: the same eventId POSTed twice yields one Log row

```bash
EV="{\"action\":\"events\",\"agentKey\":\"$KEY\",\"events\":[{\"eventId\":\"test-evt-0001\",\"type\":\"walkup_start\",\"date\":\"$D2\",\"class\":\"A\",\"slot\":1,\"rosterId\":\"rT1\",\"ts\":\"2026-01-01T08:00:00+03:00\"}]}"
post "$EV"    # → "acked":["test-evt-0001"]
post "$EV"    # → "duplicates":["test-evt-0001"]
```

The **Log** tab must contain exactly **one** `test-evt-0001` row. A wrong key is refused:

```bash
post '{"action":"events","agentKey":"wrong","events":[]}'   # → "error":"auth"
```

### 3.6 Clean up before students start booking

While nobody is booking yet, delete the test rows from **Bookings**, **Blocked** and **Log**, and the `rT1`–`rT3` rows from **Roster**. (During real use you never delete Bookings rows; see [the edit contract](#the-sheet-edit-contract).)

---

## 4. Booking page on GitHub Pages

1. On GitHub, create a **public** repository named `terminaltimeshare` and push this folder to it.
2. Edit the top of `app.js`, commit and push:

   ```js
   const EXEC_URL = 'https://script.google.com/macros/s/PASTE-YOUR-DEPLOYMENT-ID/exec';
   ```

   The URL is public by design; anyone can already read the schedule.
3. In the repo, go to **Settings → Pages**: *Source: Deploy from a branch*, *Branch: `main`*, folder `/ (root)`. Save.
4. After a minute the page is live at `https://YOUR-GITHUB-USERNAME.github.io/terminaltimeshare/`. This must match `SPA_URL` in `Code.gs`. If it doesn't, fix `SPA_URL` and redeploy a **new version** (§2.3).
5. Post the page URL next to the machine (plus the class code, if you've ticked `requireClassCode`).

Check it on a phone. The grid shows the next 5 school days; book a test slot with a test student, cancel it from the email link, and confirm "Booked, but the email didn't send" never appears.

---

## 5. Kiosk laptop install

1. **Python 3.11 or newer** from <https://www.python.org/downloads/windows/>. In the installer, keep **"tcl/tk and IDLE"** and the **py launcher** ticked; they're on by default.
2. **Git for Windows** from <https://git-scm.com/download/win>. It gives you `git` and **Git Bash**.
3. Clone to a folder that is **not synced by OneDrive** (OneDrive can lock files mid-write). Use `C:\kiosk`, not Documents or Desktop:

   ```bash
   git clone https://github.com/YOUR-GITHUB-USERNAME/terminaltimeshare.git /c/kiosk
   ```

4. In `C:\kiosk\agent`, copy `config.example.json` to **`config.json`** and fill it in. `config.json` is gitignored, so it never gets committed and `git pull` never touches it.

   ```json
   {
     "execUrl": "https://script.google.com/macros/s/PASTE-YOUR-DEPLOYMENT-ID/exec",
     "agentKey": "PASTE-THE-agentKey-FROM-THE-CONFIG-TAB",
     "passcode": "a passcode only you know",
     "pollSec": 30,
     "testWindows": []
   }
   ```

   Pick a passcode that is **not** your laptop or Google password. It's typed in front of students (the field is masked), and anyone who kills the agent via Task Manager could read this file.
5. **Start at login.** Press **Win+R**, type `shell:startup`, and press Enter. Right-click → **New → Shortcut**, set the target to `C:\kiosk\agent\watchdog.cmd`, and name it *AI Terminal kiosk*. Then right-click the shortcut → **Properties → Run: Minimized** → OK. Double-click it once now to start it.
6. **Power, once:** Control Panel → Power Options → *Choose what closing the lid does* → **Plugged in: Do nothing**. In class, keep the laptop plugged in with the lid open; closing the lid or a critical battery overrides the agent's keep-awake.
7. **Projector:** *Duplicate* (Win+P) is simplest. *Extend* also works: the agent blacks out the second screen while locked.

**How it behaves:** the agent arms at **7:25–9:05** and **12:55–2:35 PM** on school days (**Sunday–Thursday**) that aren't blocked. Outside those times it is completely invisible ("dormant").

- **To see its status:** double-click the shortcut again. A second copy can't start; it tells the running one to open a small **status window** (mode, last schedule fetch, pending log events, badges). It also offers **Arm now** (re-arm from teacher mode) and **Quit agent**, both behind the passcode. Quitting also stops the watchdog; double-click the shortcut to start again.
- **Badges** on the lock screen:
  - **TEST**: test windows are configured.
  - **OFFLINE**: 3 failed polls, or no schedule for today yet. Walk-ups still work and the passcode always works.
  - **CLOCK OFF BY n s**: fix the laptop clock.
  - **EVENTS FAILING — check agentKey**: the backend refused the log events.
  - **LOG SYNC: n pending**: events are waiting more than 5 minutes; they drain on their own when the network returns.
- **Files** in `C:\kiosk\agent` (all gitignored):
  - `config.json`
  - `cache.json`: the last schedule, valid only on the day it was fetched.
  - `session.json`: today's session state, so a crash or reboot resumes the session.
  - `outbox.json`: log events not yet delivered.
  - `agent.log`: errors and events. Look here first if anything is odd.
- **Updating:** run `git pull` in `C:\kiosk`, then quit the agent from the status window and start it again. `config.json` is untouched.

**First run inside an armed window with no cache yet:** the kiosk shows LOCKED + OFFLINE, walk-ups only, until the first successful poll. That is expected.

---

## 6. Dress rehearsal (before the first class)

Rehearse with **test students** under a **test window**. A test window arms the kiosk at a time you choose and plays *today's real bookings* for one block (by slot number) at that time. Add one to `config.json` and restart the agent:

```json
"testWindows": [{ "block": "B", "start": "10:00" }]
```

That arms 9:55–11:35 with B1 at 10:00, B2 at 10:15, … B6 at 11:15, using today's **Block B** bookings, which you can still make on the booking page because B's real slots (1:00 PM onwards) are in the future. Do it on a school-day morning. Optional fields: `"date": "YYYY-MM-DD"` (only that day) and `"slotMinutes": 15`.

- The TEST badge shows the whole time.
- Test windows are *added to* the real windows. Real class blocks still arm normally, and a test window overlapping a real block is ignored.
- Events from a rehearsal are posted against today's real Block B rows, so use **only test students**.
- Remove `testWindows` (set it to `[]`) when you're done.

Re-add the test students (`addTestStudents`), then check off each item (SPEC §11.4):

- [ ] Book on a phone → email arrives → cancel from the email link → rebook the same day.
- [ ] Don't start a booked session: at **+5:00** the slot releases to LOCKED and a `no_show` row appears in the **Log** tab (the booking's status becomes `no-show`).
- [ ] Walk-up claim from LOCKED: tap the walk-up button, pick a test name, and the session starts (Log-only, `walkup_start`).
- [ ] Walk-up re-fetch catches a just-made booking: while the kiosk shows LOCKED for, say, B3, book **B3** on a phone (its real 1:30 PM time is still in the future), then tap walk-up right away. The button fetches first, so the kiosk switches to **RESERVED** for the booker instead.
- [ ] Overtime bumped by a just-in-time phone booking: run a session into empty slots, then book the next slot on a phone with under 30 s left. The pill turns red and locks **30 s after the kiosk sees it**.
- [ ] Hard lock at block end.
- [ ] **Win+D** during LOCKED: the lock screen is back within ~1 s.
- [ ] Second monitor (Extend mode) is blacked out while LOCKED.
- [ ] Kill the agent (Task Manager → end `pythonw.exe`) mid-session. The watchdog relaunches it within ~5 s into the **same** session.
- [ ] Pull the network mid-session: timing continues from the cache, **LOG SYNC: n pending** appears after 5 min, then drains when reconnected.
- [ ] Reboot mid-session: it resumes.
- [ ] Teacher passcode during a session marks the booking `done` with the real minutes.
- [ ] Block today (date-level) with the menu: the kiosk stays dormant.
- [ ] Put a wrong `agentKey` in `config.json` and restart: **EVENTS FAILING — check agentKey** appears. Put it back.

Clean up the test rows afterwards (§3.6).

---

## 7. Teacher operations

### The Sheet edit contract

- **Bookings:** edit cell *values* only (the status, never the ids) and prefer the **Timeshare** menu for cancels. **Never sort, insert, or delete rows in Bookings.**
- **Blocked:** append-only. Add rows (use **Block a date…**); don't rearrange.
- **Roster / Config:** edit freely.

### Everyday tasks

| Situation | What to do |
|---|---|
| Force-end a session | Type the passcode on the kiosk (the pill's **Teacher** button during a session). The booking is marked `done` with the real minutes. Press **Re-arm kiosk** when finished; teacher mode also ends by itself when the next block arms. |
| Forgive a wrong no-show | Set the row's status to **`done`** (day stays used, mark cleared) or **`cancelled`** (student can rebook today). **Never set it back to `booked`**: the kiosk would immediately fire the no-show again. |
| Cancel one booking | Select its row in Bookings → **Timeshare → Cancel selected booking + email student**. |
| Day off / substitute / no-AI day | **Timeshare → Block a date…** (blank class = whole day). It lists that date's bookings and offers to cancel and email them all in one step. Blocking never silently cancels. The kiosk stays dormant on a blocked day. |
| Block one block or one slot | Same menu, with a class letter (and optionally a slot number 1–6). |
| Turn the class code on or off | Tick or untick `requireClassCode` in **Config**. Open booking pages pick it up within about a minute. |
| Fake bookings / class code leaked | Tick `requireClassCode` in **Config** if it's off, or change `classCode` if it's on. Then **Timeshare → Cancel all bookings for a date…** (optional rosterId and "created after" filters; it emails each affected student). |
| Who used it? | The **Log** tab: sessions, minutes, no-shows, walk-ups. Read it as *supervised trust*: the machine doesn't verify who is at the keyboard. |

### Rotating secrets

- **classCode:** edit Config. Takes effect immediately; post the new one.
- **agentKey:** put a new random value in Config **and** in the laptop's `config.json`, then restart the agent.
- **passcode:** edit `config.json` on the laptop, then restart the agent.

Rotate all three if `config.json` might have been seen (e.g. someone killed the agent via Task Manager), or if any secret or roster data ever reached git history. Deleting it from git doesn't remove it from history.

### Semester reset

1. **File → Make a copy** of the Sheet as the archive.
2. In the **live** Sheet: clear the data rows of Bookings and Log, replace the Roster, and rotate `classCode`.
3. On the laptop, delete `cache.json` and `outbox.json` in `C:\kiosk\agent` (quit the agent first).

Never switch to the copy: the script and its never-changing `/exec` URL belong to the original. This is also the retention policy: a minor's usage and no-show log doesn't outlive the term.

Keep the Sheet and the Apps Script project **private** to your account; never link-share them. If someone needs data, export it with the Roster and Config tabs removed.

---

## 8. For developers

```bash
cd agent && python3 -m unittest          # pure scheduling logic (core.py) + store
node backend/test/mock_test.js           # Code.gs rules against an in-memory fake Sheet (offline)
```

- `agent/core.py` holds all kiosk decisions as pure functions: slot math, armed windows, the no-show formula, overtime / just-in-time / 30-second grace, cache date-validity, blocked precedence, teacher mode and badges. `agent/agent.py` is the tkinter shell, the network threads and the Windows shims. It has no scheduling logic of its own.
- Run the agent on macOS/Linux for development with `cd agent && python3 agent.py`. That defaults to `--windowed` off Windows; Windows-only features (DPI awareness, keep-awake, chimes, multi-monitor blackout) degrade to no-ops. You need a `config.json`, plus a `testWindows` entry to make it arm. You also need a Python build with Tk: the python.org installers have it, Homebrew's `python` needs `brew install python-tk@3.x` for your version.
- Agent exit codes: `3` = another copy is running (watchdog stops), `4` = teacher quit or config error (watchdog stops), anything else → the watchdog relaunches after 5 s.
