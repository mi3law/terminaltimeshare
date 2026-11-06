/**
 * AI Terminal Timeshare — backend (Google Apps Script bound to the Google Sheet).
 *
 * Paste this whole file into the Sheet's Extensions → Apps Script editor as Code.gs.
 * Contract: SPEC.md §5 (endpoints, locking, dates, status transitions) and §7 (teacher menu).
 *
 * Web app routes (one deployment, updated in place — the /exec URL never changes):
 *   GET  ?action=schedule                          → week grid + active roster (public)
 *   POST {action:"book",   rosterId,date,class,slot,classCode}
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

// ============================================================================
// Fixed parameters (SPEC §2) — do not edit.
// ============================================================================

var SLOT_MINUTES = 15;
var SLOTS_PER_BLOCK = 6;
var WINDOW_WEEKDAYS = 5;
var BLOCK_START_MIN = { A: 7 * 60 + 30, B: 13 * 60 };   // A 7:30–9:00, B 1:00–2:30 PM
var CLASSES = ['A', 'B'];
var LOCK_WAIT_MS = 10000;

var TABS = {
  Roster:   ['rosterId', 'fullName', 'displayName', 'email', 'class', 'active'],
  Bookings: ['id', 'date', 'class', 'slot', 'rosterId', 'displayName', 'status', 'cancelToken', 'createdAt'],
  Blocked:  ['date', 'class', 'slot', 'reason'],
  Log:      ['eventId', 'timestamp', 'event', 'rosterId', 'date', 'class', 'slot', 'minutes'],
  Config:   ['classCode', 'agentKey']
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
      MailApp.sendEmail({ to: m.to, subject: m.subject, body: m.body, name: 'AI Terminal' });
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

/** The booking window: next WINDOW_WEEKDAYS calendar weekdays, including today if a weekday.
 *  Blocked dates are still included (rendered Blocked) — they never extend the window. */
function windowDates_(today) {
  var out = [];
  for (var i = 0; out.length < WINDOW_WEEKDAYS && i < 14; i++) {
    var d = addDays_(today, i);
    var wd = weekday_(d);
    if (wd !== 0 && wd !== 6) out.push(d);
  }
  return out;
}

function slotStartMin_(cls, slot) {
  return BLOCK_START_MIN[cls] + (slot - 1) * SLOT_MINUTES;
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
  return n >= 1 && n <= SLOTS_PER_BLOCK ? n : null;
}

function loadRoster_(ss) {
  var c = TABS.Roster;
  return rows_(ss, 'Roster').map(function (r) {
    return {
      rosterId: str_(r[c.indexOf('rosterId')]),
      fullName: str_(r[c.indexOf('fullName')]),
      displayName: str_(r[c.indexOf('displayName')]),
      email: str_(r[c.indexOf('email')]),
      cls: str_(r[c.indexOf('class')]).toUpperCase(),
      active: truthy_(r[c.indexOf('active')])
    };
  }).filter(function (s) { return s.rosterId && s.displayName && CLASSES.indexOf(s.cls) >= 0; });
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
    if (!b.id || !b.date || CLASSES.indexOf(b.cls) < 0 || !b.slot) return;   // skip, never throw
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
function loadBlocked_(ss) {
  var tz = tz_(ss), byDate = {}, byClass = {}, bySlot = {};
  rows_(ss, 'Blocked').forEach(function (r) {
    var date = normDate_(r[0], tz);
    if (!date) return;
    var cls = str_(r[1]).toUpperCase();
    var slotRaw = str_(r[2]);
    if (CLASSES.indexOf(cls) < 0) { byDate[date] = true; return; }
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

function loadConfig_(ss) {
  var r = rows_(ss, 'Config')[0] || [];
  return { classCode: str_(r[0]), agentKey: str_(r[1]) };
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
  var roster = loadRoster_(ss).filter(function (s) { return s.active; });
  var occ = occupancy_(loadBookings_(ss));
  var blocked = loadBlocked_(ss);

  var days = windowDates_(np.date).map(function (date) {
    return {
      date: date,
      blocked: blocked.date(date),
      classes: CLASSES.map(function (cls) {
        var slots = [];
        for (var i = 1; i <= SLOTS_PER_BLOCK; i++) {
          var startMin = slotStartMin_(cls, i);
          var slot = { i: i, start: hhmm_(startMin) };
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
        return { 'class': cls, blocked: blocked.cls(date, cls), slots: slots };
      })
    };
  });

  return {
    ok: true,
    serverTime: np.iso,
    serverEpochMs: np.epochMs,
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
  if (!rosterId || !isValidDateStr_(date) || CLASSES.indexOf(cls) < 0 ||
      !(slot >= 1 && slot <= SLOTS_PER_BLOCK && Math.floor(slot) === slot)) {
    return { ok: false, error: 'bad_request', message: 'need rosterId, date YYYY-MM-DD, class A/B, slot 1-6' };
  }

  var mail = null;
  var result = withLock_(function () {
    var tz = tz_(ss);
    var np = nowParts_(tz);
    var cfg = loadConfig_(ss);
    var student = null;
    loadRoster_(ss).forEach(function (s) { if (s.rosterId === rosterId && s.active) student = s; });

    if (!student) return { ok: false, error: 'not_on_roster' };
    if (!cfg.classCode || code.toLowerCase() !== cfg.classCode.toLowerCase()) return { ok: false, error: 'bad_code' };
    if (student.cls !== cls) return { ok: false, error: 'wrong_block' };
    if (windowDates_(np.date).indexOf(date) < 0) return { ok: false, error: 'not_in_window' };
    if (loadBlocked_(ss).slot(date, cls, slot)) return { ok: false, error: 'blocked' };
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

    if (student.email) mail = bookingEmail_(student, date, cls, slot, token);
    return { ok: true, bookingId: id, date: date, 'class': cls, slot: slot, displayName: student.displayName };
  });

  if (result.ok) result.emailSent = mail ? sendMails_([mail]) : false;   // after the lock is released
  return result;
}

function bookingEmail_(student, date, cls, slot, token) {
  var start = slotStartMin_(cls, slot);
  var when = prettyDate_(date) + ', ' + prettyTime_(start) + '–' + prettyTime_(start + SLOT_MINUTES);
  return {
    to: student.email,
    subject: 'AI terminal booked: ' + when,
    body: 'Hi ' + student.displayName + ',\n\n' +
      'You are booked on the AI terminal:\n\n' +
      '  ' + when + ' (Block ' + cls + ', slot ' + cls + slot + ')\n\n' +
      'Arrive within 5 minutes or your slot releases (and still counts as your booking for the day).\n\n' +
      'Need to cancel? Open this link before the slot starts:\n' + cancelLink_(token) + '\n\n' +
      '— AI Terminal Timeshare'
  };
}

function cancelEmail_(email, displayName, date, cls, slot, byTeacher) {
  var start = slotStartMin_(cls, slot);
  var when = prettyDate_(date) + ', ' + prettyTime_(start);
  return {
    to: email,
    subject: 'AI terminal booking cancelled: ' + when,
    body: 'Hi ' + displayName + ',\n\n' +
      'Your AI terminal booking for ' + when + ' (Block ' + cls + ') has been cancelled' +
      (byTeacher ? ' by your teacher' : '') + '. The slot is open again, and you can book another slot that day.\n\n' +
      'Booking page: ' + SPA_URL + '\n\n— AI Terminal Timeshare'
  };
}

function cancelLink_(token) {
  return SPA_URL + (SPA_URL.indexOf('?') >= 0 ? '&' : '?') + 'cancel=' + encodeURIComponent(token);
}

// ============================================================================
// POST cancel (student, via emailed token)
// ============================================================================

function bookingSummary_(b) {
  return { date: b.date, 'class': b.cls, slot: b.slot, start: hhmm_(slotStartMin_(b.cls, b.slot)),
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

  if (body.confirm !== true) {                                     // preview: read-only, no lock
    var b0 = findByToken_(ss, token);
    if (!b0) return { ok: false, error: 'invalid_token' };
    var why = cancelBlocker_(b0, nowParts_(tz_(ss)));
    return why ? { ok: false, error: why, booking: bookingSummary_(b0) }
               : { ok: true, preview: true, booking: bookingSummary_(b0) };
  }

  var mail = null;
  var result = withLock_(function () {
    var b = findByToken_(ss, token);                               // re-located inside the lock
    if (!b) return { ok: false, error: 'invalid_token' };
    var why = cancelBlocker_(b, nowParts_(tz_(ss)));
    if (why) return { ok: false, error: why, booking: bookingSummary_(b) };
    setStatusById_(ss, b.id, STATUS.CANCELLED);
    var student = rosterById_(ss, b.rosterId);
    if (student && student.email) mail = cancelEmail_(student.email, b.displayName, b.date, b.cls, b.slot, false);
    b.status = STATUS.CANCELLED;
    return { ok: true, cancelled: true, booking: bookingSummary_(b) };
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
  if (ev['class'] != null && ev['class'] !== '' && CLASSES.indexOf(ev['class']) < 0) return 'bad class';
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

  var first = ss.getSheetByName('Sheet1');
  if (first && first.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(first);

  var scriptTz = Session.getScriptTimeZone();
  var msg = 'Setup complete: tabs Roster, Bookings, Blocked, Log, Config are ready.\n\n' +
    'Sheet timezone set to ' + SCHOOL_TZ + '.\n' +
    (scriptTz === SCHOOL_TZ ? 'Script timezone matches (' + scriptTz + ').'
      : 'WARNING: the Apps Script project timezone is ' + scriptTz + ' — set "timeZone": "' + SCHOOL_TZ +
        '" in appsscript.json (Project Settings → Show "appsscript.json"), then run setup() again.') +
    '\n\nConfig has a classCode and agentKey (edit them any time). Next: fill the Roster tab.';
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
// Teacher menu (onOpen) — SPEC §5, §7
// ============================================================================

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Timeshare')
    .addItem('Cancel selected booking + email student', 'menuCancelSelected')
    .addItem('Block a date…', 'menuBlockDate')
    .addItem('Cancel all bookings for a date…', 'menuCancelAllForDate')
    .addSeparator()
    .addItem('Run setup()', 'setup')
    .addToUi();
}

function describe_(b) {
  return b.displayName + ' — ' + prettyDate_(b.date) + ' ' + prettyTime_(slotStartMin_(b.cls, b.slot)) +
    ' (' + b.cls + b.slot + ', ' + b.status + ')';
}

/** Cancels bookings by id under the lock (re-located by id at write time), then emails each
 *  affected student after release. Only 'booked' rows change. Returns {cancelled, emailed}. */
function cancelByIds_(ss, ids) {
  var mails = [];
  var res = withLock_(function () {
    var want = {};
    ids.forEach(function (id) { want[id] = true; });
    var roster = {};
    loadRoster_(ss).forEach(function (s) { roster[s.rosterId] = s; });
    var n = 0;
    loadBookings_(ss).forEach(function (b) {
      if (!want[b.id]) return;
      if (setStatusById_(ss, b.id, STATUS.CANCELLED)) {
        n++;
        var s = roster[b.rosterId];
        if (s && s.email) mails.push(cancelEmail_(s.email, b.displayName, b.date, b.cls, b.slot, true));
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
  if (ui.alert('Cancel this booking and email the student?\n\n' + describe_(b), ui.ButtonSet.YES_NO) !== ui.Button.YES) return;
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
  var cls = promptText_(ui, 'Block a date', 'Block (A or B) — leave blank to block the whole day:');
  if (cls === null) return;
  cls = cls.toUpperCase();
  if (cls && CLASSES.indexOf(cls) < 0) { ui.alert('Block must be A, B, or blank.'); return; }
  var slot = '';
  if (cls) {
    var s = promptText_(ui, 'Block a date', 'Slot number 1–6 — leave blank to block all of Block ' + cls + ':');
    if (s === null) return;
    if (s) { slot = parseSlot_(s); if (!slot) { ui.alert('Slot must be 1–6 or blank.'); return; } }
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
  var what = date + (cls ? ' Block ' + cls + (slot ? ' slot ' + slot : '') : ' (whole day)');
  if (!hits.length) { ui.alert('Blocked ' + what + '. No bookings were affected.'); return; }
  var list = hits.map(describe_).join('\n');
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
    hits.map(describe_).join('\n'), ui.ButtonSet.YES_NO);
  if (ans !== ui.Button.YES) return;
  reportCancel_(ui, cancelByIds_(ss, hits.map(function (b) { return b.id; })));
}
