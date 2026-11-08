/**
 * AI Terminal Timeshare — backend (Google Apps Script bound to the Google Sheet).
 *
 * Paste this whole file into the Sheet's Extensions → Apps Script editor as Code.gs.
 * Contract: SPEC.md §5 (endpoints, locking, dates, status transitions) and §7 (teacher menu).
 *
 * Web app routes (one deployment, updated in place — the /exec URL never changes):
 *   GET  ?action=schedule                          → week grid + active roster (public)
 *   POST {action:"book",   rosterId,date,class,slot,classCode}   (class = block id A, L or B;
 *        classCode checked only when Config.requireClassCode is ticked — schedule's classCodeRequired)
 *   POST {action:"cancel", token, confirm:false|true}   (confirm:false = preview only)
 *   POST {action:"events", agentKey, events:[...]}       (kiosk agent only)
 * POST bodies are sent as text/plain JSON (no CORS preflight). Every response is JSON.
 */

// ============================================================================
// TEACHER: edit these two constants before deploying.
// ============================================================================

/** Your school's IANA timezone. The laptop is UTC+3; CONFIRM the right zone for your city
 *  (UTC+3 examples: Asia/Riyadh, Asia/Kuwait, Asia/Qatar, Asia/Bahrain, Asia/Baghdad,
 *  Europe/Istanbul, Africa/Nairobi). Must match the Apps Script project timezone too. */
var SCHOOL_TZ = 'Asia/Riyadh';

/** Public URL of the booking page (GitHub Pages). Used for the cancel link in emails. */
var SPA_URL = 'https://YOUR-GITHUB-USERNAME.github.io/terminaltimeshare/';

/** Name used as the email sender, in subject lines, and as the email signature. */
var MAIL_NAME = 'AI Terminal';

// ============================================================================
// Fixed parameters (SPEC §2) — do not edit.
// ============================================================================

var SLOT_MINUTES = 15;
var WINDOW_DAYS = 5;                                    // school days shown/bookable
var SCHOOL_DAYS = [0, 1, 2, 3, 4];                      // Sun–Thu (0=Sun … 6=Sat); keep in sync with agent/core.py
// Blocks in time order — keep in sync with agent/core.py. A and B are class blocks: a student books
// only their own class's block (Roster.class, which may be the name set in Config.blockAName /
// blockBName). L (lunch) is open to every active student.
var BLOCKS = ['A', 'L', 'B'];
var BLOCK_START_MIN = { A: 7 * 60 + 30, L: 10 * 60 + 40, B: 13 * 60 };   // 7:30–9:00, 10:40–11:10, 1:00–2:30 PM
var BLOCK_SLOTS = { A: 6, L: 2, B: 6 };
var MAX_SLOTS = 6;                                      // most slots in any block
var CLASS_BLOCKS = ['A', 'B'];
var OPEN_BLOCKS = ['L'];
var LUNCH_LABEL = 'Lunch';
var LOCK_WAIT_MS = 10000;

var TABS = {
  Roster:   ['rosterId', 'fullName', 'displayName', 'email', 'class', 'active'],
  Bookings: ['id', 'date', 'class', 'slot', 'rosterId', 'displayName', 'status', 'cancelToken', 'createdAt'],
  Blocked:  ['date', 'class', 'slot', 'reason'],
  Log:      ['eventId', 'timestamp', 'event', 'rosterId', 'date', 'class', 'slot', 'minutes'],
  Config:   ['classCode', 'agentKey', 'requireClassCode', 'blockAName', 'blockBName']
};
/** 1-based column of the date column in each tab that has one (formatted Plain text). */
var DATE_COL = { Bookings: 2, Blocked: 1, Log: 5 };

var EVENT_TYPES = ['session_start', 'session_end', 'no_show', 'walkup_start', 'teacher_unlock', 'rearm'];
var STATUS = { BOOKED: 'booked', DONE: 'done', NO_SHOW: 'no-show', CANCELLED: 'cancelled' };
var MAX_EVENTS_PER_POST = 100;

// ============================================================================
// Web app entry points
// ============================================================================

function doGet(e) {
  try {
    var action = (e && e.parameter && e.parameter.action) || 'schedule';
    if (action !== 'schedule') return json_({ ok: false, error: 'bad_request', message: 'unknown action' });
    return json_(buildSchedule_(SpreadsheetApp.getActiveSpreadsheet()));
  } catch (err) {
    console.error(err && err.stack || err);
    return json_({ ok: false, error: 'server_error', message: String(err && err.message || err) });
  }
}

function doPost(e) {
  try {
    var body;
    try {
      body = JSON.parse((e && e.postData && e.postData.contents) || '');
    } catch (parseErr) {
      return json_({ ok: false, error: 'bad_request', message: 'body must be JSON' });
    }
    if (!body || typeof body !== 'object') return json_({ ok: false, error: 'bad_request' });
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    switch (body.action) {
      case 'book':   return json_(handleBook_(ss, body));
      case 'cancel': return json_(handleCancel_(ss, body));
      case 'events': return json_(handleEvents_(ss, body));
      default:       return json_({ ok: false, error: 'bad_request', message: 'unknown action' });
    }
  } catch (err) {
    console.error(err && err.stack || err);
    return json_({ ok: false, error: 'server_error', message: String(err && err.message || err) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ============================================================================
// Locking (SPEC §5): waitLock(10 s) → JSON busy; flush() before release; email after release.
// ============================================================================

/** Runs fn() under the script lock. Returns fn's result, or a busy error if the lock wasn't won. */
function withLock_(fn) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(LOCK_WAIT_MS);
  } catch (lockErr) {
    return { ok: false, error: 'busy', message: 'busy, try again' };
  }
  try {
    var result = fn();
    SpreadsheetApp.flush();            // commit writes BEFORE another execution can read
    return result;
  } finally {
    lock.releaseLock();
  }
}

/** Sends queued emails (after the lock is released). Returns true if all sent. */
function sendMails_(mails) {
  var allOk = true;
  (mails || []).forEach(function (m) {
    try {
      MailApp.sendEmail({ to: m.to, subject: m.subject, body: m.body, name: MAIL_NAME });
    } catch (mailErr) {
      allOk = false;
      console.error('MailApp failed for ' + m.to + ': ' + mailErr);
    }
  });
  return allOk;
}

// ============================================================================
// Dates and time (SPEC §2, §5 "Dates in Sheets")
// ============================================================================

var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function tz_(ss) {
  return ss.getSpreadsheetTimeZone() || SCHOOL_TZ;
}

function isDateObj_(v) {
  return Object.prototype.toString.call(v) === '[object Date]';
}

/** True if s is YYYY-MM-DD and a real calendar date. */
function isValidDateStr_(s) {
  if (typeof s !== 'string' || !DATE_RE.test(s)) return false;
  var p = s.split('-').map(Number);
  var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
  return d.getUTCFullYear() === p[0] && d.getUTCMonth() === p[1] - 1 && d.getUTCDate() === p[2];
}

/** Normalizes a Sheet cell (Date object or string) to 'YYYY-MM-DD', or null if invalid. Never throws. */
function normDate_(v, tz) {
  try {
    if (isDateObj_(v)) return isNaN(v.getTime()) ? null : Utilities.formatDate(v, tz, 'yyyy-MM-dd');
    var s = String(v == null ? '' : v).trim().replace(/^'/, '');
    return isValidDateStr_(s) ? s : null;
  } catch (e) {
    return null;
  }
}

/** Current school-local wall clock: {date:'YYYY-MM-DD', secOfDay, iso, epochMs}. */
function nowParts_(tz) {
  var now = new Date();
  var stamp = Utilities.formatDate(now, tz, "yyyy-MM-dd'T'HH:mm:ss");
  var hms = stamp.substr(11).split(':').map(Number);
  return {
    date: stamp.substr(0, 10),
    secOfDay: hms[0] * 3600 + hms[1] * 60 + hms[2],
    iso: Utilities.formatDate(now, tz, "yyyy-MM-dd'T'HH:mm:ssXXX"),
    epochMs: now.getTime()
  };
}

/** Day of week (0=Sun) of a 'YYYY-MM-DD' string — pure calendar math, no timezone involved. */
function weekday_(dateStr) {
  var p = dateStr.split('-').map(Number);
  return new Date(Date.UTC(p[0], p[1] - 1, p[2])).getUTCDay();
}

function addDays_(dateStr, n) {
  var p = dateStr.split('-').map(Number);
  var d = new Date(Date.UTC(p[0], p[1] - 1, p[2] + n));
  return d.getUTCFullYear() + '-' + pad2_(d.getUTCMonth() + 1) + '-' + pad2_(d.getUTCDate());
}

function pad2_(n) { return (n < 10 ? '0' : '') + n; }

/** The booking window: next WINDOW_DAYS school days (SCHOOL_DAYS), including today if it is one.
 *  Blocked dates are still included (rendered Blocked) — they never extend the window. */
function windowDates_(today) {
  var out = [];
  for (var i = 0; out.length < WINDOW_DAYS && i < 14; i++) {
    var d = addDays_(today, i);
    if (SCHOOL_DAYS.indexOf(weekday_(d)) >= 0) out.push(d);
  }
  return out;
}

function slotStartMin_(cls, slot) {
  return BLOCK_START_MIN[cls] + (slot - 1) * SLOT_MINUTES;
}

function slotsIn_(cls) { return BLOCK_SLOTS[cls] || 0; }

function isSlotOf_(cls, slot) { return slot >= 1 && slot <= slotsIn_(cls) && Math.floor(slot) === slot; }

/** What people see: Config's class names (e.g. CD / EF) or "Block A" / "Block B", and "Lunch". */
function blockLabels_(cfg) {
  return { A: cfg.blockNames.A || 'Block A', L: LUNCH_LABEL, B: cfg.blockNames.B || 'Block B' };
}

/** Short slot name for the grid and teacher lists: "CD3", "Lunch 1", or "A3" when no name is set. */
function slotCode_(cfg, cls, slot) {
  var short = blockLabels_(cfg)[cls];
  if (short === 'Block ' + cls) short = cls;
  return short + (short.length > 3 || /\d$/.test(short) ? ' ' : '') + slot;   // "P1" → "P1 3", not "P13"
}

/** A typed block/class value → 'A' | 'L' | 'B', or '' if unrecognized. Accepts the letters, "Lunch",
 *  and the class names in Config (blockAName → A, blockBName → B), ignoring case and spaces. */
function blockOf_(v, cfg) {
  var s = str_(v).toUpperCase();
  if (!s) return '';
  if (BLOCKS.indexOf(s) >= 0) return s;
  if (s === LUNCH_LABEL.toUpperCase()) return 'L';
  for (var i = 0; i < CLASS_BLOCKS.length; i++) {
    var name = str_(cfg.blockNames[CLASS_BLOCKS[i]]).toUpperCase();
    if (name && name === s) return CLASS_BLOCKS[i];
  }
  return '';
}

function hhmm_(min) {
  return pad2_(Math.floor(min / 60)) + ':' + pad2_(min % 60);
}

function prettyTime_(min) {
  var h = Math.floor(min / 60), m = min % 60;
  return ((h + 11) % 12 + 1) + ':' + pad2_(m) + (h < 12 ? ' AM' : ' PM');
}

function prettyDate_(dateStr) {
  var p = dateStr.split('-').map(Number);
  var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
  return Utilities.formatDate(d, 'UTC', 'EEE MMM d');
}

/** True if the slot (date, startMin) starts strictly after now. */
function startsInFuture_(dateStr, startMin, np) {
  if (dateStr > np.date) return true;
  if (dateStr < np.date) return false;
  return startMin * 60 > np.secOfDay;
}

/** True if the slot ending at endMin on dateStr has ended (end before now). */
function hasEnded_(dateStr, endMin, np) {
  if (dateStr < np.date) return true;
  if (dateStr > np.date) return false;
  return endMin * 60 <= np.secOfDay;
}

// ============================================================================
// Sheet access
// ============================================================================

function sheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) throw new Error('Missing tab "' + name + '" — run setup() first.');
  return sh;
}

/** Data rows (no header) of a tab, exactly TABS[name].length columns wide. */
function rows_(ss, name) {
  var sh = sheet_(ss, name);
  var last = sh.getLastRow();
  if (last < 2) return [];
  return sh.getRange(2, 1, last - 1, TABS[name].length).getValues();
}

/** Appends rows, extending the sheet if needed and forcing the date column to Plain text. */
function appendRows_(sh, rows, dateCol) {
  if (!rows.length) return;
  var start = sh.getLastRow() + 1;
  var overflow = start + rows.length - 1 - sh.getMaxRows();
  if (overflow > 0) sh.insertRowsAfter(sh.getMaxRows(), overflow);
  if (dateCol) sh.getRange(start, dateCol, rows.length, 1).setNumberFormat('@');
  sh.getRange(start, 1, rows.length, rows[0].length).setValues(rows);
}

function str_(v) { return String(v == null ? '' : v).trim(); }

function truthy_(v) {
  if (v === true || v === 1) return true;
  var s = str_(v).toLowerCase();
  return s === 'true' || s === 'yes' || s === 'y' || s === '1' || s === 'x';
}

function parseSlot_(v) {
  var m = str_(v).match(/(\d+)\s*$/);
  if (!m) return null;
  var n = Number(m[1]);
  return n >= 1 && n <= MAX_SLOTS ? n : null;
}

/** Roster rows a student can book from; cls is the class block 'A' or 'B' (see blockOf_). */
function loadRoster_(ss, cfg) {
  cfg = cfg || loadConfig_(ss);
  var c = TABS.Roster;
  return rows_(ss, 'Roster').map(function (r) {
    return {
      rosterId: str_(r[c.indexOf('rosterId')]),
      fullName: str_(r[c.indexOf('fullName')]),
      displayName: str_(r[c.indexOf('displayName')]),
      email: str_(r[c.indexOf('email')]),
      cls: blockOf_(r[c.indexOf('class')], cfg),
      active: truthy_(r[c.indexOf('active')])
    };
  }).filter(function (s) { return s.rosterId && s.displayName && CLASS_BLOCKS.indexOf(s.cls) >= 0; });
}

/** Bookings with normalized fields. Rows whose date/class/slot fail validation are skipped. */
function loadBookings_(ss) {
  var tz = tz_(ss), c = TABS.Bookings, out = [];
  rows_(ss, 'Bookings').forEach(function (r, idx) {
    var b = {
      row: idx + 2,
      id: str_(r[c.indexOf('id')]),
      date: normDate_(r[c.indexOf('date')], tz),
      cls: str_(r[c.indexOf('class')]).toUpperCase(),
      slot: parseSlot_(r[c.indexOf('slot')]),
      rosterId: str_(r[c.indexOf('rosterId')]),
      displayName: str_(r[c.indexOf('displayName')]),
      status: str_(r[c.indexOf('status')]).toLowerCase(),
      cancelToken: str_(r[c.indexOf('cancelToken')]),
      createdAt: r[c.indexOf('createdAt')]
    };
    if (!b.id || !b.date || BLOCKS.indexOf(b.cls) < 0 || !isSlotOf_(b.cls, b.slot)) return;   // skip, never throw
    out.push(b);
  });
  return out;
}

/** Any non-cancelled status occupies the slot; unknown hand-typed statuses count as booked. */
function occupyingStatus_(status) {
  if (status === STATUS.CANCELLED) return null;
  if (status === STATUS.DONE || status === STATUS.NO_SHOW) return status;
  return STATUS.BOOKED;
}

/** Blocked tab → matcher. Unrecognized class/slot values widen the block (blocked wins). */
function loadBlocked_(ss, cfg) {
  cfg = cfg || loadConfig_(ss);
  var tz = tz_(ss), byDate = {}, byClass = {}, bySlot = {};
  rows_(ss, 'Blocked').forEach(function (r) {
    var date = normDate_(r[0], tz);
    if (!date) return;
    var cls = blockOf_(r[1], cfg);
    var slotRaw = str_(r[2]);
    if (!cls) { byDate[date] = true; return; }
    var slot = slotRaw ? parseSlot_(slotRaw) : null;
    if (!slot) { byClass[date + '|' + cls] = true; return; }
    bySlot[date + '|' + cls + '|' + slot] = true;
  });
  return {
    date: function (d) { return !!byDate[d]; },
    cls: function (d, c) { return !!byDate[d] || !!byClass[d + '|' + c]; },
    slot: function (d, c, s) { return !!byDate[d] || !!byClass[d + '|' + c] || !!bySlot[d + '|' + c + '|' + s]; }
  };
}

/** requireClassCode: ticked/TRUE → booking needs the class code; blank/FALSE → it doesn't.
 *  blockAName / blockBName: the class names for the 7:30 and 1:00 blocks (blank = "Block A/B"). */
function loadConfig_(ss) {
  var r = rows_(ss, 'Config')[0] || [];
  return { classCode: str_(r[0]), agentKey: str_(r[1]), requireClassCode: truthy_(r[2]),
           blockNames: { A: str_(r[3]), B: str_(r[4]) } };
}

function slotKey_(date, cls, slot) { return date + '|' + cls + '|' + slot; }

/** Map slotKey → occupying booking (prefers a 'booked' row if several non-cancelled rows exist). */
function occupancy_(bookings) {
  var map = {};
  bookings.forEach(function (b) {
    var st = occupyingStatus_(b.status);
    if (!st) return;
    var k = slotKey_(b.date, b.cls, b.slot);
    if (!map[k] || (st === STATUS.BOOKED && occupyingStatus_(map[k].status) !== STATUS.BOOKED)) map[k] = b;
  });
  return map;
}

// ============================================================================
// GET schedule
// ============================================================================

function buildSchedule_(ss) {
  var tz = tz_(ss);
  var np = nowParts_(tz);
  var cfg = loadConfig_(ss);
  var labels = blockLabels_(cfg);
  var roster = loadRoster_(ss, cfg).filter(function (s) { return s.active; });
  var occ = occupancy_(loadBookings_(ss));
  var blocked = loadBlocked_(ss, cfg);

  var days = windowDates_(np.date).map(function (date) {
    return {
      date: date,
      blocked: blocked.date(date),
      classes: BLOCKS.map(function (cls) {
        var slots = [];
        for (var i = 1; i <= slotsIn_(cls); i++) {
          var startMin = slotStartMin_(cls, i);
          var slot = { i: i, start: hhmm_(startMin), code: slotCode_(cfg, cls, i) };
          if (blocked.slot(date, cls, i)) {
            slot.status = 'blocked';                       // blocked wins (SPEC §6)
          } else if (hasEnded_(date, startMin + SLOT_MINUTES, np)) {
            slot.status = 'past';                          // privacy: no name, no outcome
          } else {
            var b = occ[slotKey_(date, cls, i)];
            slot.status = b ? occupyingStatus_(b.status) : 'open';
            if (b) slot.displayName = b.displayName;
          }
          slots.push(slot);
        }
        // open: any student may book it (lunch), not only the roster class `cls`
        return { 'class': cls, label: labels[cls], open: OPEN_BLOCKS.indexOf(cls) >= 0,
                 blocked: blocked.cls(date, cls), slots: slots };
      })
    };
  });

  return {
    ok: true,
    serverTime: np.iso,
    serverEpochMs: np.epochMs,
    classCodeRequired: cfg.requireClassCode,
    roster: roster.map(function (s) { return { rosterId: s.rosterId, displayName: s.displayName, 'class': s.cls }; }),
    days: days
  };
}

// ============================================================================
// POST book
// ============================================================================

function handleBook_(ss, body) {
  var rosterId = str_(body.rosterId);
  var date = str_(body.date);
  var cls = str_(body['class']).toUpperCase();
  var slot = Number(body.slot);
  var code = str_(body.classCode);
  if (!rosterId || !isValidDateStr_(date) || BLOCKS.indexOf(cls) < 0 || !isSlotOf_(cls, slot)) {
    return { ok: false, error: 'bad_request',
             message: 'need rosterId, date YYYY-MM-DD, class A/L/B, slot 1-6 (1-2 for L)' };
  }

  var mail = null;
  var result = withLock_(function () {
    var tz = tz_(ss);
    var np = nowParts_(tz);
    var cfg = loadConfig_(ss);
    var student = null;
    loadRoster_(ss, cfg).forEach(function (s) { if (s.rosterId === rosterId && s.active) student = s; });

    if (!student) return { ok: false, error: 'not_on_roster' };
    if (cfg.requireClassCode &&
        (!cfg.classCode || code.toLowerCase() !== cfg.classCode.toLowerCase())) return { ok: false, error: 'bad_code' };
    if (OPEN_BLOCKS.indexOf(cls) < 0 && student.cls !== cls) return { ok: false, error: 'wrong_block' };
    if (windowDates_(np.date).indexOf(date) < 0) return { ok: false, error: 'not_in_window' };
    if (loadBlocked_(ss, cfg).slot(date, cls, slot)) return { ok: false, error: 'blocked' };
    var startMin = slotStartMin_(cls, slot);
    if (!startsInFuture_(date, startMin, np)) return { ok: false, error: 'past' };

    var bookings = loadBookings_(ss);
    var taken = bookings.some(function (b) {
      return b.date === date && b.cls === cls && b.slot === slot && occupyingStatus_(b.status);
    });
    if (taken) return { ok: false, error: 'slot_taken' };
    var hasDay = bookings.some(function (b) {
      return b.date === date && b.rosterId === rosterId && occupyingStatus_(b.status);
    });
    if (hasDay) return { ok: false, error: 'already_booked_today' };

    var id = 'b-' + Utilities.getUuid().slice(0, 8);
    var token = Utilities.getUuid();
    appendRows_(sheet_(ss, 'Bookings'),
      [[id, date, cls, slot, rosterId, student.displayName, STATUS.BOOKED, token, new Date()]],
      DATE_COL.Bookings);

    if (student.email) mail = bookingEmail_(student, date, cls, slot, token, blockLabels_(cfg)[cls]);
    return { ok: true, bookingId: id, date: date, 'class': cls, slot: slot, displayName: student.displayName };
  });

  if (result.ok) result.emailSent = mail ? sendMails_([mail]) : false;   // after the lock is released
  return result;
}

/** label: the block as people know it (blockLabels_), e.g. "CD" or "Lunch". */
function bookingEmail_(student, date, cls, slot, token, label) {
  var start = slotStartMin_(cls, slot);
  var when = prettyDate_(date) + ', ' + prettyTime_(start) + '–' + prettyTime_(start + SLOT_MINUTES);
  return {
    to: student.email,
    subject: MAIL_NAME + ' booked: ' + when,
    body: 'Hi ' + student.displayName + ',\n\n' +
      'You are booked on the ' + MAIL_NAME + ':\n\n' +
      '  ' + when + ' (' + label + ', slot ' + slot + ')\n\n' +
      'Arrive within 5 minutes or your slot releases (and still counts as your booking for the day).\n\n' +
      'Need to cancel? Open this link before the slot starts:\n' + cancelLink_(token) + '\n\n' +
      '— ' + MAIL_NAME
  };
}

function cancelEmail_(email, displayName, date, cls, slot, byTeacher, label) {
  var start = slotStartMin_(cls, slot);
  var when = prettyDate_(date) + ', ' + prettyTime_(start);
  return {
    to: email,
    subject: MAIL_NAME + ' booking cancelled: ' + when,
    body: 'Hi ' + displayName + ',\n\n' +
      'Your ' + MAIL_NAME + ' booking for ' + when + ' (' + label + ') has been cancelled' +
      (byTeacher ? ' by your teacher' : '') + '. The slot is open again, and you can book another slot that day.\n\n' +
      'Booking page: ' + SPA_URL + '\n\n— ' + MAIL_NAME
  };
}

function cancelLink_(token) {
  return SPA_URL + (SPA_URL.indexOf('?') >= 0 ? '&' : '?') + 'cancel=' + encodeURIComponent(token);
}

// ============================================================================
// POST cancel (student, via emailed token)
// ============================================================================

function bookingSummary_(b, cfg) {
  return { date: b.date, 'class': b.cls, slot: b.slot, start: hhmm_(slotStartMin_(b.cls, b.slot)),
           code: slotCode_(cfg, b.cls, b.slot), label: blockLabels_(cfg)[b.cls],
           displayName: b.displayName, status: occupyingStatus_(b.status) || STATUS.CANCELLED };
}

/** Error code if booking b can't be cancelled by the student now, else null. */
function cancelBlocker_(b, np) {
  var st = occupyingStatus_(b.status);
  if (!st) return 'already_cancelled';
  if (st !== STATUS.BOOKED) return 'started';
  if (!startsInFuture_(b.date, slotStartMin_(b.cls, b.slot), np)) return 'started';
  return null;
}

function findByToken_(ss, token) {
  var found = null;
  loadBookings_(ss).forEach(function (b) { if (b.cancelToken && b.cancelToken === token) found = b; });
  return found;
}

function handleCancel_(ss, body) {
  var token = str_(body.token);
  if (!token) return { ok: false, error: 'invalid_token' };
  var cfg = loadConfig_(ss);

  if (body.confirm !== true) {                                     // preview: read-only, no lock
    var b0 = findByToken_(ss, token);
    if (!b0) return { ok: false, error: 'invalid_token' };
    var why = cancelBlocker_(b0, nowParts_(tz_(ss)));
    return why ? { ok: false, error: why, booking: bookingSummary_(b0, cfg) }
               : { ok: true, preview: true, booking: bookingSummary_(b0, cfg) };
  }

  var mail = null;
  var result = withLock_(function () {
    var b = findByToken_(ss, token);                               // re-located inside the lock
    if (!b) return { ok: false, error: 'invalid_token' };
    var why = cancelBlocker_(b, nowParts_(tz_(ss)));
    if (why) return { ok: false, error: why, booking: bookingSummary_(b, cfg) };
    setStatusById_(ss, b.id, STATUS.CANCELLED);
    var student = rosterById_(ss, b.rosterId);
    if (student && student.email) {
      mail = cancelEmail_(student.email, b.displayName, b.date, b.cls, b.slot, false, blockLabels_(cfg)[b.cls]);
    }
    b.status = STATUS.CANCELLED;
    return { ok: true, cancelled: true, booking: bookingSummary_(b, cfg) };
  });
  if (result.ok) result.emailSent = mail ? sendMails_([mail]) : false;
  return result;
}

function rosterById_(ss, rosterId) {
  var hit = null;
  loadRoster_(ss).forEach(function (s) { if (s.rosterId === rosterId) hit = s; });
  return hit;
}

/** Re-locates the booking row BY ID at write time (call inside the lock) and applies the
 *  status-transition guard: only booked → done | no-show | cancelled. Returns true if written. */
function setStatusById_(ss, id, newStatus) {
  var sh = sheet_(ss, 'Bookings');
  var last = sh.getLastRow();
  if (last < 2) return false;
  var colStatus = TABS.Bookings.indexOf('status') + 1;
  var ids = sh.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < ids.length; i++) {
    if (str_(ids[i][0]) !== id) continue;
    var cell = sh.getRange(i + 2, colStatus);
    if (occupyingStatus_(str_(cell.getValue()).toLowerCase()) !== STATUS.BOOKED) return false;  // terminal: never rewrite
    cell.setValue(newStatus);
    return true;
  }
  return false;
}

// ============================================================================
// POST events (kiosk agent)
// ============================================================================

var EVENT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
var ROSTER_ID_RE = /^[A-Za-z0-9_.-]{1,40}$/;

/** Returns an error string for a malformed event, or null. */
function validateEvent_(ev) {
  if (!ev || typeof ev !== 'object') return 'not an object';
  if (!EVENT_ID_RE.test(str_(ev.eventId))) return 'bad eventId';
  if (EVENT_TYPES.indexOf(ev.type) < 0) return 'bad type';
  if (ev.date != null && ev.date !== '' && !isValidDateStr_(ev.date)) return 'bad date';
  if (ev['class'] != null && ev['class'] !== '' && BLOCKS.indexOf(ev['class']) < 0) return 'bad class';
  if (ev.slot != null && ev.slot !== '' && !parseSlot_(ev.slot)) return 'bad slot';
  if (ev.rosterId != null && ev.rosterId !== '' && !ROSTER_ID_RE.test(str_(ev.rosterId))) return 'bad rosterId';
  if (ev.minutes != null && ev.minutes !== '' && !(typeof ev.minutes === 'number' && ev.minutes >= 0 && ev.minutes < 1000)) return 'bad minutes';
  if ((ev.type === 'session_end' || ev.type === 'no_show') && (!ev.date || !ev['class'] || !ev.slot)) return 'missing date/class/slot';
  return null;
}

function handleEvents_(ss, body) {
  var cfg0 = loadConfig_(ss);
  if (!cfg0.agentKey || str_(body.agentKey) !== cfg0.agentKey) return { ok: false, error: 'auth' };
  var events = body.events;
  if (!Array.isArray(events) || events.length > MAX_EVENTS_PER_POST) {
    return { ok: false, error: 'bad_request', message: 'events must be an array of at most ' + MAX_EVENTS_PER_POST };
  }

  return withLock_(function () {
    var logSh = sheet_(ss, 'Log');
    var seen = {};
    rows_(ss, 'Log').forEach(function (r) { var id = str_(r[0]); if (id) seen[id] = true; });
    var bookings = loadBookings_(ss);
    var acked = [], duplicates = [], rejected = [], newRows = [];

    events.forEach(function (ev) {
      var id = ev && str_(ev.eventId);
      if (id && seen[id]) { duplicates.push(id); return; }          // idempotent: silently ack
      var err = validateEvent_(ev);
      if (err) { rejected.push({ eventId: id || null, error: err }); return; }

      var slot = ev.slot ? parseSlot_(ev.slot) : '';
      var rosterId = str_(ev.rosterId);
      if (ev.type === 'session_start' || ev.type === 'session_end' || ev.type === 'no_show') {
        var b = resolveBooking_(bookings, ev.date, ev['class'], slot);
        if (b) {
          var sameStudent = !rosterId || rosterId === b.rosterId;
          if (!rosterId) rosterId = b.rosterId;
          var target = ev.type === 'session_end' ? STATUS.DONE : ev.type === 'no_show' ? STATUS.NO_SHOW : null;
          // A walk-up's session_end (different rosterId) never marks someone else's booking done.
          if (target && sameStudent && occupyingStatus_(b.status) === STATUS.BOOKED) {
            if (setStatusById_(ss, b.id, target)) b.status = target;
          }
        }
      }
      var ts = new Date(ev.ts);
      newRows.push([id, isNaN(ts.getTime()) ? new Date() : ts, ev.type, rosterId,
                    ev.date || '', ev['class'] || '', slot, ev.minutes == null ? '' : ev.minutes]);
      seen[id] = true;
      acked.push(id);
    });

    appendRows_(logSh, newRows, DATE_COL.Log);
    return { ok: true, acked: acked, duplicates: duplicates, rejected: rejected };
  });
}

/** The booking row for (date, class, slot): the 'booked' one if any, else another non-cancelled one. */
function resolveBooking_(bookings, date, cls, slot) {
  var hit = null;
  bookings.forEach(function (b) {
    if (b.date !== date || b.cls !== cls || b.slot !== slot) return;
    var st = occupyingStatus_(b.status);
    if (!st) return;
    if (!hit || (st === STATUS.BOOKED && occupyingStatus_(hit.status) !== STATUS.BOOKED)) hit = b;
  });
  return hit;
}

// ============================================================================
// setup() — run once from the Apps Script editor (SPEC §10 step 2)
// ============================================================================

function setup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.setSpreadsheetTimeZone(SCHOOL_TZ);

  Object.keys(TABS).forEach(function (name) {
    var sh = ss.getSheetByName(name) || ss.insertSheet(name);
    var headers = TABS[name];
    sh.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sh.setFrozenRows(1);
    if (DATE_COL[name]) sh.getRange(1, DATE_COL[name], sh.getMaxRows(), 1).setNumberFormat('@');  // Plain text
  });

  var bookings = sheet_(ss, 'Bookings');
  bookings.getRange(1, TABS.Bookings.indexOf('createdAt') + 1, bookings.getMaxRows(), 1)
    .setNumberFormat('yyyy-mm-dd hh:mm:ss');
  var log = sheet_(ss, 'Log');
  log.getRange(1, TABS.Log.indexOf('timestamp') + 1, log.getMaxRows(), 1).setNumberFormat('yyyy-mm-dd hh:mm:ss');

  var cfg = sheet_(ss, 'Config');
  cfg.getRange(2, 1, 1, 2).setNumberFormat('@');
  if (!str_(cfg.getRange(2, 1).getValue())) cfg.getRange(2, 1).setValue(randomCode_(6));
  if (!str_(cfg.getRange(2, 2).getValue())) cfg.getRange(2, 2).setValue(Utilities.getUuid().replace(/-/g, ''));
  if (!str_(cfg.getRange(2, 3).getValue())) cfg.getRange(2, 3).insertCheckboxes();   // unticked = no code asked

  var first = ss.getSheetByName('Sheet1');
  if (first && first.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(first);

  var scriptTz = Session.getScriptTimeZone();
  var msg = 'Setup complete: tabs Roster, Bookings, Blocked, Log, Config are ready.\n\n' +
    'Sheet timezone set to ' + SCHOOL_TZ + '.\n' +
    (scriptTz === SCHOOL_TZ ? 'Script timezone matches (' + scriptTz + ').'
      : 'WARNING: the Apps Script project timezone is ' + scriptTz + ' — set "timeZone": "' + SCHOOL_TZ +
        '" in appsscript.json (Project Settings → Show "appsscript.json"), then run setup() again.') +
    '\n\nConfig has a classCode and agentKey (edit them any time). Tick requireClassCode to make students ' +
    'type the class code when booking (unticked = not asked). Optional: type your class names under ' +
    'blockAName (the 7:30 class) and blockBName (the 1:00 class); the Roster class column can then use them. ' +
    'Next: fill the Roster tab, then run Timeshare → Check roster.';
  console.log(msg);
  try { SpreadsheetApp.getUi().alert(msg); } catch (noUi) { /* run from editor without a Sheet UI */ }
}

function randomCode_(n) {
  var alphabet = 'abcdefghjkmnpqrstuvwxyz23456789', s = '';
  for (var i = 0; i < n; i++) s += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
  return s;
}

/** Optional: adds three FAKE students (using your own email) for the README curl tests.
 *  Delete their Roster rows (and the test Bookings/Log/Blocked rows) before real use. */
function addTestStudents() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var me = Session.getEffectiveUser().getEmail();
  appendRows_(sheet_(ss, 'Roster'), [
    ['rT1', 'Test Alpha', 'Test Alpha', me, 'A', true],
    ['rT2', 'Test Bravo', 'Test Bravo', me, 'A', true],
    ['rT3', 'Test Charlie', 'Test Charlie', me, 'B', true]
  ], null);
}

// ============================================================================
// Roster check — why a student is missing from the booking page's name list
// ============================================================================

/** Reports which Roster rows the booking page skips, and why (Timeshare menu or the editor).
 *  Read-only. Names rows by number; the report stays in your Sheet and Apps Script log. */
function checkRoster() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = sheet_(ss, 'Roster');
  var cols = TABS.Roster;
  var col = function (name) { return String.fromCharCode(65 + cols.indexOf(name)) + ' (' + name + ')'; };
  var lines = ['Roster check for the Sheet "' + ss.getName() + '".', ''];

  var header = sh.getRange(1, 1, 1, cols.length).getValues()[0];
  var badHeaders = [];
  cols.forEach(function (name, i) {
    if (str_(header[i]).toLowerCase() !== name.toLowerCase()) {
      badHeaders.push('Column ' + String.fromCharCode(65 + i) + ' is headed ' + rosterValue_(header[i]) +
        ', expected "' + name + '".');
    }
  });
  if (badHeaders.length) {
    lines = lines.concat(badHeaders, ['The booking page reads columns by position, not by header, so they must be ' +
      'in this order: ' + cols.join(', ') + '.', '']);
  }

  var cfg = loadConfig_(ss), labels = blockLabels_(cfg);
  var named = !!(cfg.blockNames.A || cfg.blockNames.B);
  var noId = [], noName = [], badClass = [], inactive = [], classVals = [], activeVals = [];
  var noEmail = [], ids = {}, names = {}, total = 0, shown = { A: 0, B: 0 };
  rows_(ss, 'Roster').forEach(function (r, i) {
    var row = i + 2;
    if (r.every(function (v) { return str_(v) === ''; })) return;   // blank row: ignored
    total++;
    var id = str_(r[cols.indexOf('rosterId')]);
    var name = str_(r[cols.indexOf('displayName')]);
    var clsRaw = r[cols.indexOf('class')];
    var cls = blockOf_(clsRaw, cfg);
    var activeRaw = r[cols.indexOf('active')];
    var ok = true;
    if (id) (ids[id] = ids[id] || []).push(row);
    if (!id) { noId.push(row); ok = false; }
    if (!name) { noName.push(row); ok = false; }
    if (CLASS_BLOCKS.indexOf(cls) < 0) { badClass.push(row); addDistinct_(classVals, rosterValue_(clsRaw)); ok = false; }
    if (!truthy_(activeRaw)) { inactive.push(row); addDistinct_(activeVals, rosterValue_(activeRaw)); ok = false; }
    if (!ok) return;
    shown[cls]++;
    if (!str_(r[cols.indexOf('email')])) noEmail.push(row);
    var key = name.toLowerCase();                       // Lunch lists every student, so names clash roster-wide
    (names[key] = names[key] || []).push(row);
  });

  if (!total) {
    lines.push('The Roster tab has no students: rows 2 and below are empty. If you filled in the roster ' +
      'somewhere else (another tab or a copy of this Sheet), the booking page can\'t see it. It only reads ' +
      'this Sheet\'s Roster tab.');
  } else {
    var nShown = shown.A + shown.B, nSkipped = total - nShown;
    var split = '(' + shown.A + ' in ' + labels.A + ', ' + shown.B + ' in ' + labels.B + ')';
    lines.push(nSkipped
      ? total + ' students on the Roster tab: ' + nShown + ' show on the booking page ' + split + ', ' +
        nSkipped + (nSkipped === 1 ? ' is' : ' are') + ' skipped.'
      : 'All ' + total + ' students show on the booking page ' + split + '.');
    var reason = function (rows, what, found, fix) {
      if (!rows.length) return;
      lines.push('', '• ' + what + ': ' + rowsText_(rows) + '.' + (found ? ' Found: ' + found.join(', ') + '.' : ''),
        '  Fix: ' + fix);
    };
    reason(inactive, 'Column ' + col('active') + ' isn\'t TRUE, so the student is hidden', activeVals,
      'type TRUE (or tick a checkbox) for every student who should appear. FALSE hides a student on purpose.');
    reason(badClass, 'Column ' + col('class') + ' isn\'t a class this system knows', classVals, named
      ? 'use ' + labels.A + ' (the 7:30 block) or ' + labels.B + ' (the 1:00 block), as named in the Config tab. ' +
        'The letters A and B work too.'
      : 'use A (the 7:30 block) or B (the 1:00 block). To use your own class names instead (like "CD"), type ' +
        'them in the Config tab: blockAName for the 7:30 class, blockBName for the 1:00 class. ' +
        'Run setup() first if Config doesn\'t have those columns yet.');
    reason(noId, 'Column ' + col('rosterId') + ' is empty', null, 'give each student a made-up id like r01.');
    reason(noName, 'Column ' + col('displayName') + ' is empty', null,
      'add the short name shown on the grid, like "Maya R.".');
  }

  var warnings = [];
  if (noEmail.length) {
    warnings.push('Column ' + col('email') + ' is empty on ' + rowsText_(noEmail) + '. They can book, but get no ' +
      'confirmation email or cancel link.');
  }
  Object.keys(ids).forEach(function (id) {
    if (ids[id].length > 1) warnings.push('rosterId "' + id + '" is on ' + rowsText_(ids[id]) + '. Each id must be unique.');
  });
  Object.keys(names).forEach(function (key) {
    if (names[key].length > 1) {
      warnings.push(rowsText_(names[key]) + ' have the same displayName, so students can\'t tell them apart in ' +
        'the name list (Lunch lists everyone). Make them different ("Maya Ro." / "Maya Re.").');
    }
  });
  if (warnings.length) {
    lines.push('', 'Also worth fixing:');
    warnings.forEach(function (w) { lines.push('• ' + w); });
  }

  lines.push('', 'Changes to the Sheet take effect right away: reload the booking page to see them.');
  var msg = lines.join('\n');
  console.log(msg);
  try { SpreadsheetApp.getUi().alert('Roster check', msg, SpreadsheetApp.getUi().ButtonSet.OK); } catch (noUi) { /* editor */ }
  return msg;
}

/** A cell value as the report shows it. */
function rosterValue_(v) {
  if (v === true) return 'TRUE';
  if (v === false) return 'FALSE (unticked)';
  var s = str_(v);
  if (!s) return '(blank)';
  return '"' + (s.length > 20 ? s.slice(0, 20) + '…' : s) + '"';
}

function addDistinct_(list, v) {
  if (list.indexOf(v) < 0 && list.length < 5) list.push(v);
}

/** [2,3,4,7] → "rows 2–4, 7". */
function rowsText_(nums) {
  var parts = [];
  for (var i = 0; i < nums.length; i++) {
    var j = i;
    while (j + 1 < nums.length && nums[j + 1] === nums[j] + 1) j++;
    parts.push(j > i ? nums[i] + '–' + nums[j] : String(nums[i]));
    i = j;
  }
  return (nums.length === 1 ? 'row ' : 'rows ') + parts.join(', ');
}

// ============================================================================
// Teacher menu (onOpen) — SPEC §5, §7
// ============================================================================

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Timeshare')
    .addItem('Cancel selected booking + email student', 'menuCancelSelected')
    .addItem('Block a date…', 'menuBlockDate')
    .addItem('Cancel all bookings for a date…', 'menuCancelAllForDate')
    .addSeparator()
    .addItem('Check roster', 'checkRoster')
    .addItem('Run setup()', 'setup')
    .addToUi();
}

/** One booking as a line for teacher dialogs; usage: bookings.map(describer_(cfg)). */
function describer_(cfg) {
  return function (b) {
    return b.displayName + ' — ' + prettyDate_(b.date) + ' ' + prettyTime_(slotStartMin_(b.cls, b.slot)) +
      ' (' + slotCode_(cfg, b.cls, b.slot) + ', ' + b.status + ')';
  };
}

/** Cancels bookings by id under the lock (re-located by id at write time), then emails each
 *  affected student after release. Only 'booked' rows change. Returns {cancelled, emailed}. */
function cancelByIds_(ss, ids) {
  var mails = [];
  var res = withLock_(function () {
    var want = {};
    ids.forEach(function (id) { want[id] = true; });
    var cfg = loadConfig_(ss), labels = blockLabels_(cfg);
    var roster = {};
    loadRoster_(ss, cfg).forEach(function (s) { roster[s.rosterId] = s; });
    var n = 0;
    loadBookings_(ss).forEach(function (b) {
      if (!want[b.id]) return;
      if (setStatusById_(ss, b.id, STATUS.CANCELLED)) {
        n++;
        var s = roster[b.rosterId];
        if (s && s.email) mails.push(cancelEmail_(s.email, b.displayName, b.date, b.cls, b.slot, true, labels[b.cls]));
      }
    });
    return { ok: true, cancelled: n };
  });
  if (!res.ok) return { cancelled: 0, emailed: false, busy: true };
  return { cancelled: res.cancelled, emailed: sendMails_(mails) };
}

function reportCancel_(ui, r) {
  if (r.busy) { ui.alert('The backend is busy — try again in a few seconds.'); return; }
  ui.alert('Cancelled ' + r.cancelled + ' booking(s).' + (r.emailed ? '' : '\nSome emails failed to send — check the execution log.'));
}

function menuCancelSelected() {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getActiveSheet();
  if (sh.getName() !== 'Bookings') { ui.alert('Select a row in the Bookings tab first.'); return; }
  var row = sh.getActiveRange().getRow();
  if (row < 2) { ui.alert('Select a booking row (not the header).'); return; }
  var id = str_(sh.getRange(row, 1).getValue());
  var b = null;
  loadBookings_(ss).forEach(function (x) { if (x.id === id) b = x; });
  if (!b) { ui.alert('That row is not a valid booking.'); return; }
  if (occupyingStatus_(b.status) !== STATUS.BOOKED) {
    ui.alert('Only "booked" rows can be cancelled. This one is "' + b.status + '".\n' +
      'To forgive a no-show, set the status to done or cancelled by hand (never back to booked).');
    return;
  }
  var line = describer_(loadConfig_(ss))(b);
  if (ui.alert('Cancel this booking and email the student?\n\n' + line, ui.ButtonSet.YES_NO) !== ui.Button.YES) return;
  reportCancel_(ui, cancelByIds_(ss, [b.id]));                    // id re-located inside the lock
}

function promptText_(ui, title, prompt) {
  var r = ui.prompt(title, prompt, ui.ButtonSet.OK_CANCEL);
  return r.getSelectedButton() === ui.Button.OK ? str_(r.getResponseText()) : null;
}

function menuBlockDate() {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var date = promptText_(ui, 'Block a date', 'Date to block (YYYY-MM-DD):');
  if (date === null) return;
  if (!isValidDateStr_(date)) { ui.alert('"' + date + '" is not a valid YYYY-MM-DD date.'); return; }
  var cfg = loadConfig_(ss), labels = blockLabels_(cfg);
  var choices = labels.A + ', ' + labels.L + ' or ' + labels.B;
  var typed = promptText_(ui, 'Block a date', 'Block (' + choices + ') — leave blank to block the whole day:');
  if (typed === null) return;
  var cls = blockOf_(typed, cfg);
  if (typed && !cls) { ui.alert('Block must be ' + choices + ', or blank.'); return; }
  var slot = '';
  if (cls) {
    var max = slotsIn_(cls);
    var s = promptText_(ui, 'Block a date', 'Slot number 1–' + max + ' — leave blank to block all of ' + labels[cls] + ':');
    if (s === null) return;
    if (s) { slot = parseSlot_(s); if (!slot || slot > max) { ui.alert('Slot must be 1–' + max + ' or blank.'); return; } }
  }
  var reason = promptText_(ui, 'Block a date', 'Reason (optional):');
  if (reason === null) return;

  var wrote = withLock_(function () {
    appendRows_(sheet_(ss, 'Blocked'), [[date, cls, slot, reason]], DATE_COL.Blocked);   // canonical strings
    return { ok: true };
  });
  if (!wrote.ok) { ui.alert('The backend is busy — try again.'); return; }

  var hits = loadBookings_(ss).filter(function (b) {
    return b.date === date && (!cls || b.cls === cls) && (!slot || b.slot === slot) &&
      occupyingStatus_(b.status) === STATUS.BOOKED;
  });
  var what = date + (cls ? ' ' + labels[cls] + (slot ? ' slot ' + slot : '') : ' (whole day)');
  if (!hits.length) { ui.alert('Blocked ' + what + '. No bookings were affected.'); return; }
  var list = hits.map(describer_(cfg)).join('\n');
  var ans = ui.alert('Blocked ' + what + '.\n\nThese bookings are still active:\n' + list +
    '\n\nCancel all of them and email the students?', ui.ButtonSet.YES_NO);
  if (ans !== ui.Button.YES) {
    ui.alert('Left them as-is. They show as Blocked on the grid; cancel them later from the Bookings tab.');
    return;
  }
  reportCancel_(ui, cancelByIds_(ss, hits.map(function (b) { return b.id; })));
}

function menuCancelAllForDate() {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var date = promptText_(ui, 'Cancel all bookings for a date', 'Date (YYYY-MM-DD):');
  if (date === null) return;
  if (!isValidDateStr_(date)) { ui.alert('"' + date + '" is not a valid YYYY-MM-DD date.'); return; }
  var rid = promptText_(ui, 'Cancel all bookings for a date', 'Only this rosterId (optional — blank = everyone):');
  if (rid === null) return;
  var after = promptText_(ui, 'Cancel all bookings for a date',
    'Only bookings created after (optional, "YYYY-MM-DD HH:mm" school time — blank = any time):');
  if (after === null) return;
  var afterDate = null;
  if (after) {
    try { afterDate = Utilities.parseDate(after, tz_(ss), 'yyyy-MM-dd HH:mm'); } catch (e) { afterDate = null; }
    if (!afterDate) { ui.alert('Could not read "' + after + '" — use YYYY-MM-DD HH:mm.'); return; }
  }
  var hits = loadBookings_(ss).filter(function (b) {
    if (b.date !== date || occupyingStatus_(b.status) !== STATUS.BOOKED) return false;
    if (rid && b.rosterId !== rid) return false;
    if (afterDate) {
      var c = isDateObj_(b.createdAt) ? b.createdAt : new Date(b.createdAt);
      if (isNaN(c.getTime()) || c.getTime() <= afterDate.getTime()) return false;
    }
    return true;
  });
  if (!hits.length) { ui.alert('No active bookings match.'); return; }
  var ans = ui.alert('Cancel these ' + hits.length + ' booking(s) and email each student?\n\n' +
    hits.map(describer_(loadConfig_(ss))).join('\n'), ui.ButtonSet.YES_NO);
  if (ans !== ui.Button.YES) return;
  reportCancel_(ui, cancelByIds_(ss, hits.map(function (b) { return b.id; })));
}
