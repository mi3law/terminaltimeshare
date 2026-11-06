// Offline logic test for backend/Code.gs against an in-memory fake of the Sheets/Apps Script APIs.
// Developer convenience only — the authoritative checks are the live curl tests in README.md.
// Run:  node backend/test/mock_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const crypto = require('crypto');

const TZ = 'Asia/Riyadh';
let fakeNowMs = Date.parse('2026-11-09T08:05:00+03:00');   // Monday 08:05 school time

class FakeDate extends Date {
  constructor(...args) { if (args.length === 0) super(fakeNowMs); else super(...args); }
  static now() { return fakeNowMs; }
}

// ---- tiny formatter for the Utilities.formatDate patterns Code.gs uses ----
function parts(date, tz) {
  const f = new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'short', month2: undefined });
  const o = {};
  f.formatToParts(date).forEach(p => { o[p.type] = p.value; });
  const mon = new Intl.DateTimeFormat('en-US', { timeZone: tz, month: 'short' }).format(date);
  return { ...o, mon };
}
function offset(date, tz) {
  const p = parts(date, tz);
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  const mins = Math.round((asUtc - Math.floor(date.getTime() / 1000) * 1000) / 60000);
  const sign = mins >= 0 ? '+' : '-', a = Math.abs(mins);
  return sign + String(Math.floor(a / 60)).padStart(2, '0') + ':' + String(a % 60).padStart(2, '0');
}
function formatDate(date, tz, pat) {
  const p = parts(date, tz);
  const map = {
    'yyyy-MM-dd': `${p.year}-${p.month}-${p.day}`,
    "yyyy-MM-dd'T'HH:mm:ss": `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}`,
    "yyyy-MM-dd'T'HH:mm:ssXXX": `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}${offset(date, tz)}`,
    'EEE MMM d': `${p.weekday} ${p.mon} ${+p.day}`,
  };
  if (!(pat in map)) throw new Error('unsupported pattern ' + pat);
  return map[pat];
}

// ---- fake Sheets ----
class Range {
  constructor(sh, r, c, nr, nc) { Object.assign(this, { sh, r, c, nr, nc }); }
  getValues() {
    const out = [];
    for (let i = 0; i < this.nr; i++) {
      const row = [];
      for (let j = 0; j < this.nc; j++) row.push(this.sh.get(this.r + i, this.c + j));
      out.push(row);
    }
    return out;
  }
  getValue() { return this.sh.get(this.r, this.c); }
  setValues(v) {
    v.forEach((row, i) => row.forEach((x, j) => this.sh.set(this.r + i, this.c + j, x)));
    return this;
  }
  setValue(x) { this.sh.set(this.r, this.c, x); return this; }
  setNumberFormat(f) {
    for (let i = 0; i < this.nr; i++) for (let j = 0; j < this.nc; j++) this.sh.fmt[`${this.r + i},${this.c + j}`] = f;
    return this;
  }
  setFontWeight() { return this; }
  getRow() { return this.r; }
}
class Sheet {
  constructor(name) { this.name = name; this.cells = {}; this.fmt = {}; this.maxRows = 1000; }
  getName() { return this.name; }
  get(r, c) { const v = this.cells[`${r},${c}`]; return v === undefined ? '' : v; }
  set(r, c, x) {
    if (r > this.maxRows) throw new Error('out of bounds row ' + r);
    // Emulate Sheets auto-coercion of typed date strings unless the cell is Plain text.
    if (typeof x === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(x) && this.fmt[`${r},${c}`] !== '@') {
      x = new FakeDate(Date.parse(x + 'T00:00:00+03:00'));
    }
    this.cells[`${r},${c}`] = x;
  }
  getRange(r, c, nr = 1, nc = 1) { return new Range(this, r, c, nr, nc); }
  getLastRow() {
    let m = 0;
    Object.keys(this.cells).forEach(k => { const r = +k.split(',')[0]; if (this.cells[k] !== '' && r > m) m = r; });
    return m;
  }
  getMaxRows() { return this.maxRows; }
  insertRowsAfter(_, n) { this.maxRows += n; }
  setFrozenRows() {}
}
class Spreadsheet {
  constructor() { this.sheets = [new Sheet('Sheet1')]; this.tz = 'Etc/UTC'; }
  getSheetByName(n) { return this.sheets.find(s => s.name === n) || null; }
  insertSheet(n) { const s = new Sheet(n); this.sheets.push(s); return s; }
  getSheets() { return this.sheets; }
  deleteSheet(s) { this.sheets = this.sheets.filter(x => x !== s); }
  getSpreadsheetTimeZone() { return this.tz; }
  setSpreadsheetTimeZone(tz) { this.tz = tz; }
}

const ss = new Spreadsheet();
const sent = [];
let lockFree = true;
const ctx = {
  Date: FakeDate, Math, JSON, String, Number, Object, Array, isNaN, encodeURIComponent, console,
  SpreadsheetApp: { getActiveSpreadsheet: () => ss, flush: () => {}, getUi: () => { throw new Error('no ui'); } },
  LockService: { getScriptLock: () => ({
    waitLock: () => { if (!lockFree) throw new Error('timeout'); },
    releaseLock: () => {},
  }) },
  Utilities: { formatDate, getUuid: () => crypto.randomUUID() },
  MailApp: { sendEmail: m => { if (m.to === 'broken@example.com') throw new Error('mail down'); sent.push(m); } },
  Session: { getScriptTimeZone: () => TZ, getEffectiveUser: () => ({ getEmail: () => 'teacher@example.com' }) },
  ContentService: { createTextOutput: s => ({ s, setMimeType() { return this; } }), MimeType: { JSON: 'json' } },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'Code.gs'), 'utf8'), ctx);

const post = body => JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify(body) } }).s);
const get = () => JSON.parse(ctx.doGet({ parameter: { action: 'schedule' } }).s);
const slotOf = (sch, date, cls, i) => sch.days.find(d => d.date === date).classes.find(c => c.class === cls).slots[i - 1];
let pass = 0;
function check(name, fn) { fn(); pass++; console.log('ok  -', name); }

// ---- setup + fixtures (fake names only) ----
ctx.setup();
const roster = ss.getSheetByName('Roster');
[['rT1', 'Test Alpha', 'Test Alpha', 'alpha@example.com', 'A', true],
 ['rT2', 'Test Bravo', 'Test Bravo', 'bravo@example.com', 'A', true],
 ['rT3', 'Test Charlie', 'Test Charlie', 'broken@example.com', 'B', true],
 ['rT4', 'Test Delta', 'Test Delta', 'delta@example.com', 'A', false]]
  .forEach((r, i) => roster.getRange(i + 2, 1, 1, 6).setValues([r]));
const CODE = ss.getSheetByName('Config').getRange(2, 1).getValue();
const KEY = ss.getSheetByName('Config').getRange(2, 2).getValue();
const book = (rosterId, date, cls, slot, classCode = CODE) => post({ action: 'book', rosterId, date, class: cls, slot, classCode });

check('setup creates exact headers and timezone', () => {
  assert.deepStrictEqual(ss.getSheetByName('Bookings').getRange(1, 1, 1, 9).getValues()[0],
    ['id', 'date', 'class', 'slot', 'rosterId', 'displayName', 'status', 'cancelToken', 'createdAt']);
  assert.strictEqual(ss.tz, TZ);
  assert.ok(!ss.getSheetByName('Sheet1'));
  assert.ok(CODE && KEY);
});

check('schedule window = 5 weekdays from Monday', () => {
  const s = get();
  assert.deepStrictEqual(s.days.map(d => d.date), ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13']);
  assert.strictEqual(s.roster.length, 3, 'inactive student hidden');
  assert.ok(!JSON.stringify(s).includes('@'), 'no emails in public schedule');
  assert.strictEqual(s.serverTime, '2026-11-09T08:05:00+03:00');
});

check('window skips the weekend when today is Friday', () => {
  const saved = fakeNowMs;
  fakeNowMs = Date.parse('2026-11-13T10:00:00+03:00');
  assert.deepStrictEqual(get().days.map(d => d.date), ['2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19']);
  fakeNowMs = Date.parse('2026-11-14T10:00:00+03:00');   // Saturday: today excluded
  assert.strictEqual(get().days[0].date, '2026-11-16');
  fakeNowMs = saved;
});

let tokenT1;
check('booked date round-trips as the identical YYYY-MM-DD string', () => {
  const r = book('rT1', '2026-11-10', 'A', 6);
  assert.strictEqual(r.ok, true, JSON.stringify(r));
  assert.strictEqual(r.emailSent, true);
  const cell = ss.getSheetByName('Bookings').getRange(2, 2).getValue();
  assert.strictEqual(cell, '2026-11-10', 'stored as plain text, not a Date');
  const s = slotOf(get(), '2026-11-10', 'A', 6);
  assert.deepStrictEqual([s.status, s.displayName], ['booked', 'Test Alpha']);
  tokenT1 = ss.getSheetByName('Bookings').getRange(2, 8).getValue();
  assert.ok(sent[0].body.includes('?cancel=' + tokenT1));
  assert.ok(sent[0].body.includes('Arrive within 5 minutes'));
});

check('a Date-object date cell (hand-typed) still normalizes on read', () => {
  const b = ss.getSheetByName('Bookings');
  b.fmt['3,2'] = 'auto';
  b.getRange(3, 1, 1, 9).setValues([['b-hand', '2026-11-11', 'A', 1, 'rT2', 'Test Bravo', 'booked', 'tok-hand', '']]);
  assert.ok(b.getRange(3, 2).getValue() instanceof Date, 'fake sheet coerced it');
  assert.strictEqual(slotOf(get(), '2026-11-11', 'A', 1).displayName, 'Test Bravo');
});

check('garbage date rows are skipped, not thrown on', () => {
  const b = ss.getSheetByName('Bookings');
  b.getRange(4, 1, 1, 9).setValues([['b-bad', 'next tuesday', 'A', 2, 'rT2', 'Test Bravo', 'booked', 'tok-bad', '']]);
  assert.strictEqual(get().ok, true);
});

check('slot just taken / one booking per day / class code / block / roster / window', () => {
  assert.strictEqual(book('rT2', '2026-11-10', 'A', 6).error, 'slot_taken');
  assert.strictEqual(book('rT1', '2026-11-10', 'A', 5).error, 'already_booked_today');
  assert.strictEqual(book('rT2', '2026-11-10', 'A', 5, 'nope').error, 'bad_code');
  assert.strictEqual(book('rT2', '2026-11-10', 'B', 5).error, 'wrong_block');
  assert.strictEqual(book('rT4', '2026-11-10', 'A', 5).error, 'not_on_roster');
  assert.strictEqual(book('rZZ', '2026-11-10', 'A', 5).error, 'not_on_roster');
  assert.strictEqual(book('rT2', '2026-11-16', 'A', 5).error, 'not_in_window');
  assert.strictEqual(book('rT2', '2026-11-10', 'A', 9).error, 'bad_request');
});

check('slot start must be in the future (today 08:05: A3 08:00 started, A4 08:15 ok)', () => {
  assert.strictEqual(book('rT2', '2026-11-09', 'A', 3).error, 'past');
  const r = book('rT2', '2026-11-09', 'A', 4);
  assert.strictEqual(r.ok, true);
});

check('privacy: ended slots are "past" with no name; current slot keeps name', () => {
  const b = ss.getSheetByName('Bookings');
  const n = b.getLastRow() + 1;
  b.fmt[`${n},2`] = '@';
  b.getRange(n, 1, 1, 9).setValues([['b-old', '2026-11-09', 'A', 1, 'rT1', 'Test Alpha', 'no-show', 't-old', '']]);
  b.fmt[`${n + 1},2`] = '@';
  b.getRange(n + 1, 1, 1, 9).setValues([['b-cur', '2026-11-09', 'A', 3, 'rT3', 'Test Charlie', 'done', 't-cur', '']]);
  const s = get();
  assert.deepStrictEqual(slotOf(s, '2026-11-09', 'A', 1), { i: 1, start: '07:30', status: 'past' });
  assert.deepStrictEqual(slotOf(s, '2026-11-09', 'A', 3), { i: 3, start: '08:00', status: 'done', displayName: 'Test Charlie' });
});

check('blocked at every granularity (date, date+class, date+class+slot) and precedence', () => {
  const bl = ss.getSheetByName('Blocked');
  bl.getRange(2, 1, 3, 4).setValues([
    ['2026-11-11', '', '', 'assembly'],
    ['2026-11-12', 'A', '', 'field trip'],
    ['2026-11-13', 'A', 2, 'maintenance']]);
  assert.strictEqual(book('rT2', '2026-11-11', 'A', 4).error, 'blocked');
  assert.strictEqual(book('rT3', '2026-11-11', 'B', 4).error, 'blocked');
  assert.strictEqual(book('rT2', '2026-11-12', 'A', 4).error, 'blocked');
  assert.strictEqual(book('rT2', '2026-11-13', 'A', 2).error, 'blocked');
  assert.strictEqual(book('rT3', '2026-11-12', 'B', 4).ok, true, 'other block same date still open');
  assert.strictEqual(book('rT2', '2026-11-13', 'A', 3).ok, true, 'other slot same block still open');
  const s = get();
  const d11 = s.days.find(d => d.date === '2026-11-11');
  assert.strictEqual(d11.blocked, true);
  assert.strictEqual(slotOf(s, '2026-11-11', 'A', 1).status, 'blocked', 'blocked wins over a booked row');
  assert.strictEqual(s.days.find(d => d.date === '2026-11-12').classes[0].blocked, true);
  assert.strictEqual(slotOf(s, '2026-11-13', 'A', 2).status, 'blocked');
});

check('email failure still books, with emailSent:false', () => {
  const r = book('rT3', '2026-11-13', 'B', 1);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.emailSent, false);
});

check('busy lock returns JSON busy error', () => {
  lockFree = false;
  assert.strictEqual(book('rT2', '2026-11-10', 'A', 1).error, 'busy');
  lockFree = true;
});

check('cancel: preview, confirm, already cancelled, invalid, rebook frees day', () => {
  const p = post({ action: 'cancel', token: tokenT1 });
  assert.strictEqual(p.ok, true); assert.strictEqual(p.preview, true);
  assert.strictEqual(p.booking.displayName, 'Test Alpha');
  const c = post({ action: 'cancel', token: tokenT1, confirm: true });
  assert.strictEqual(c.ok, true);
  assert.strictEqual(post({ action: 'cancel', token: tokenT1, confirm: true }).error, 'already_cancelled');
  assert.strictEqual(post({ action: 'cancel', token: 'nope', confirm: true }).error, 'invalid_token');
  assert.strictEqual(slotOf(get(), '2026-11-10', 'A', 6).status, 'open');
  assert.strictEqual(book('rT1', '2026-11-10', 'A', 2).ok, true, 'cancelled booking frees the day');
});

check('cancel after slot start is rejected', () => {
  const b = ss.getSheetByName('Bookings');
  const n = b.getLastRow() + 1;
  b.fmt[`${n},2`] = '@';
  b.getRange(n, 1, 1, 9).setValues([['b-started', '2026-11-09', 'B', 1, 'rT3', 'Test Charlie', 'booked', 'tok-started', '']]);
  fakeNowMs = Date.parse('2026-11-09T13:02:00+03:00');
  assert.strictEqual(post({ action: 'cancel', token: 'tok-started', confirm: true }).error, 'started');
  assert.strictEqual(post({ action: 'cancel', token: 'tok-started' }).error, 'started');
  fakeNowMs = Date.parse('2026-11-09T08:05:00+03:00');
});

const statusOf = id => {
  const b = ss.getSheetByName('Bookings');
  for (let r = 2; r <= b.getLastRow(); r++) if (b.getRange(r, 1).getValue() === id) return b.getRange(r, 7).getValue();
};
const logRows = () => ss.getSheetByName('Log').getLastRow() - 1;
const ev = (o) => ({ ts: '2026-11-09T08:10:00+03:00', ...o });

check('events: auth required', () => {
  assert.strictEqual(post({ action: 'events', agentKey: 'wrong', events: [] }).error, 'auth');
});

check('events: same eventId twice yields one Log row', () => {
  const body = { action: 'events', agentKey: KEY, events: [ev({ eventId: 'test-evt-0001', type: 'walkup_start', date: '2026-11-09', class: 'A', slot: 2, rosterId: 'rT1' })] };
  const before = logRows();
  const r1 = post(body), r2 = post(body);
  assert.deepStrictEqual(r1.acked, ['test-evt-0001']);
  assert.deepStrictEqual(r2.duplicates, ['test-evt-0001']);
  assert.strictEqual(logRows(), before + 1);
  assert.strictEqual(ss.getSheetByName('Log').getRange(logRows() + 1, 5).getValue(), '2026-11-09');
});

check('events: transitions booked→done / booked→no-show; terminal rows never rewritten', () => {
  // b-hand: 2026-11-11 A1 booked (Date cell) → no_show
  let r = post({ action: 'events', agentKey: KEY, events: [ev({ eventId: 'e-ns', type: 'no_show', date: '2026-11-11', class: 'A', slot: 1 })] });
  assert.deepStrictEqual(r.acked, ['e-ns']);
  assert.strictEqual(statusOf('b-hand'), 'no-show');
  assert.strictEqual(ss.getSheetByName('Log').getRange(logRows() + 1, 4).getValue(), 'rT2', 'rosterId filled from booking');
  r = post({ action: 'events', agentKey: KEY, events: [ev({ eventId: 'e-late', type: 'session_end', date: '2026-11-11', class: 'A', slot: 1, minutes: 9 })] });
  assert.deepStrictEqual(r.acked, ['e-late'], 'logged');
  assert.strictEqual(statusOf('b-hand'), 'no-show', 'terminal state untouched');
});

check('events: a walk-up session_end (other rosterId) never marks the booking done', () => {
  // A4 today booked by rT2 earlier
  post({ action: 'events', agentKey: KEY, events: [ev({ eventId: 'e-wu', type: 'session_end', date: '2026-11-09', class: 'A', slot: 4, rosterId: 'rT1', minutes: 3 })] });
  const id = (() => { const b = ss.getSheetByName('Bookings'); for (let r = 2; r <= b.getLastRow(); r++) if (b.getRange(r, 2).getValue() === '2026-11-09' && b.getRange(r, 4).getValue() === 4) return b.getRange(r, 1).getValue(); })();
  assert.strictEqual(statusOf(id), 'booked');
  post({ action: 'events', agentKey: KEY, events: [ev({ eventId: 'e-own', type: 'session_end', date: '2026-11-09', class: 'A', slot: 4, rosterId: 'rT2', minutes: 14.5 })] });
  assert.strictEqual(statusOf(id), 'done');
});

check('events: malformed events rejected individually, good ones acked', () => {
  const r = post({ action: 'events', agentKey: KEY, events: [
    ev({ eventId: 'e-good', type: 'rearm', date: '2026-11-09', class: 'A', slot: null }),
    ev({ eventId: 'e-bad', type: 'explode' }),
    ev({ eventId: '=HYPERLINK("x")', type: 'rearm' })] });
  assert.deepStrictEqual(r.acked, ['e-good']);
  assert.strictEqual(r.rejected.length, 2);
});

check('teacher cancelByIds_ only touches booked rows', () => {
  const res = ctx.cancelByIds_(ss, ['b-hand', 'b-started']);
  assert.strictEqual(res.cancelled, 1);
  assert.strictEqual(statusOf('b-hand'), 'no-show');
  assert.strictEqual(statusOf('b-started'), 'cancelled');
});

console.log(`\n${pass} backend mock checks passed`);
