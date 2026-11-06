// =============================================================================
// TEACHER: paste your Apps Script web-app /exec URL here (public by design).
// =============================================================================
const EXEC_URL = 'PASTE_YOUR_APPS_SCRIPT_EXEC_URL_HERE';
// =============================================================================

// AI Terminal Booking — vanilla SPA. Contract: SPEC.md §4 (and §5 for the API).
'use strict';

const POLL_MS = 60000;
const POLL_JITTER_MS = 10000;
const SLOT_MINUTES = 15;
const BLOCK_LABEL = { A: 'Block A · 7:30–9:00 AM', B: 'Block B · 1:00–2:30 PM' };
const LS_NAME = 'tts.myName';
const LS_CODE = 'tts.classCode';

const BOOK_ERRORS = {
  slot_taken: 'That slot was just taken — pick another one.',
  already_booked_today: 'You already have a booking that day (1 per day).',
  bad_code: 'Wrong class code.',
  wrong_block: "That name isn't in this block's class.",
  not_on_roster: "That name isn't in this block's class.",
  busy: 'The booking system is busy — try again in a few seconds.',
  blocked: 'That slot is blocked.',
  past: 'That slot has already started.',
  not_in_window: "That date isn't open for booking yet.",
  bad_request: 'Something was off with that request — reload the page and try again.',
  unreachable: "Can't reach the booking system — show your teacher this message.",
};
const CANCEL_ERRORS = {
  invalid_token: "This cancel link isn't valid.",
  already_cancelled: 'This booking is already cancelled.',
  started: 'This slot has already started — a no-show releases it automatically. See your teacher if needed.',
  busy: 'The booking system is busy — try again in a few seconds.',
  unreachable: "Can't reach the booking system — show your teacher this message.",
};

let schedule = null;          // last good schedule response
let fetchedAtPerf = 0;        // performance.now() when `schedule` arrived
let lastGoodAt = null;        // Date (device clock) of last good fetch, for the stamp
let lastError = false;
let pollTimer = null;
let inFlight = false;
let pending = null;           // {date, cls, slot, start} being booked

const $ = (id) => document.getElementById(id);
const configured = () => /^https:\/\//.test(EXEC_URL);

// ---------------------------------------------------------------- network ---

/** Calls the backend; resolves to parsed JSON, or {ok:false,error:'unreachable'} on any failure. */
async function api(method, body) {
  try {
    const opts = method === 'GET'
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: JSON.stringify(body) };
    const url = method === 'GET' ? `${EXEC_URL}?action=schedule&_=${Date.now()}` : EXEC_URL;
    const res = await fetch(url, opts);
    const text = await res.text();
    const data = JSON.parse(text);              // Google's HTML error pages land in the catch
    return data && typeof data === 'object' ? data : { ok: false, error: 'unreachable' };
  } catch (e) {
    return { ok: false, error: 'unreachable' };
  }
}

async function refresh() {
  if (!configured() || inFlight) return;
  inFlight = true;
  const data = await api('GET');
  inFlight = false;
  if (data.ok && Array.isArray(data.days)) {
    schedule = data;
    fetchedAtPerf = performance.now();
    lastGoodAt = new Date();
    lastError = false;
  } else {
    lastError = true;                            // keep rendering the last good grid
  }
  render();
}

function scheduleNextPoll() {
  clearTimeout(pollTimer);
  const delay = POLL_MS + (Math.random() * 2 - 1) * POLL_JITTER_MS;
  pollTimer = setTimeout(async () => { await refresh(); scheduleNextPoll(); }, delay);
}

// ------------------------------------------------------------- school time ---

/** School-local "now" as {date, sec}: the server's wall clock plus time elapsed since the fetch. */
function schoolNow() {
  const st = schedule.serverTime;                       // e.g. 2026-11-09T08:05:12+03:00
  const [h, m, s] = st.substr(11, 8).split(':').map(Number);
  let sec = h * 3600 + m * 60 + s + (performance.now() - fetchedAtPerf) / 1000;
  let date = st.substr(0, 10);
  if (sec >= 86400) { date = addDays(date, Math.floor(sec / 86400)); sec %= 86400; }
  return { date, sec };
}

function addDays(dateStr, n) {
  const [y, mo, d] = dateStr.split('-').map(Number);
  const t = new Date(Date.UTC(y, mo - 1, d + n));
  return t.toISOString().slice(0, 10);
}

function toMin(hhmm) { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; }

function prettyTime(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
}

function prettyDate(dateStr) {
  const [y, mo, d] = dateStr.split('-').map(Number);
  return new Date(Date.UTC(y, mo - 1, d))
    .toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
}

/** Compares a slot on dateStr starting at startMin to now: 'future' | 'current' | 'ended'. */
function phase(dateStr, startMin, now) {
  if (dateStr > now.date) return 'future';
  if (dateStr < now.date) return 'ended';
  if (now.sec < startMin * 60) return 'future';
  if (now.sec < (startMin + SLOT_MINUTES) * 60) return 'current';
  return 'ended';
}

// ------------------------------------------------------------------ render ---

function render() {
  renderStamp();
  if (!schedule) {
    $('grid').innerHTML = '';
    if (lastError) showBanner(BOOK_ERRORS.unreachable, 'error');
    return;
  }
  const myName = safeGet(LS_NAME);
  const now = schoolNow();
  const grid = $('grid');
  grid.textContent = '';

  for (const day of schedule.days) {
    const card = el('article', 'day');
    card.appendChild(el('h2', '', prettyDate(day.date)));
    if (day.blocked) {
      card.classList.add('blocked');
      card.appendChild(el('p', 'note', 'Blocked — no AI terminal today'));
      grid.appendChild(card);
      continue;
    }
    for (const cls of day.classes) {
      const block = el('section', 'block');
      block.appendChild(el('h3', '', BLOCK_LABEL[cls.class] || `Block ${cls.class}`));
      const list = el('ol');
      for (const slot of cls.slots) list.appendChild(el('li')).appendChild(slotCell(day.date, cls.class, slot, now, myName));
      block.appendChild(list);
      card.appendChild(block);
    }
    grid.appendChild(card);
  }
}

function slotCell(date, cls, slot, now, myName) {
  const when = phase(date, toMin(slot.start), now);
  let label, kind;
  if (slot.status === 'blocked') { label = 'Blocked'; kind = 'blocked'; }
  else if (slot.status === 'past' || when === 'ended') { label = 'Past'; kind = 'past'; }
  else if (slot.status === 'done' || slot.status === 'no-show') { label = 'Released'; kind = 'released'; }
  else if (slot.status === 'booked') {
    const mine = myName && slot.displayName === myName;
    label = mine ? `Mine — ${slot.displayName}` : `Booked — ${slot.displayName || ''}`;
    kind = mine ? 'mine' : 'booked';
  } else if (when === 'current') { label = 'Now — walk-up at the machine'; kind = 'now'; }
  else { label = 'Open'; kind = 'open'; }

  const cell = el(kind === 'open' ? 'button' : 'div', `slot ${kind}`);
  cell.appendChild(el('span', 't', `${cls}${slot.i} · ${prettyTime(slot.start)}`));
  cell.appendChild(el('span', 's', label));
  if (kind === 'open') {
    cell.type = 'button';
    cell.setAttribute('aria-label', `Book ${cls}${slot.i}, ${prettyDate(date)} ${prettyTime(slot.start)}`);
    cell.addEventListener('click', () => openBooking(date, cls, slot));
  }
  return cell;
}

function renderStamp() {
  const u = $('updated');
  if (!configured()) { u.textContent = 'Not set up yet: EXEC_URL in app.js is still the placeholder.'; u.className = 'updated stale'; return; }
  if (!lastGoodAt) { u.textContent = lastError ? 'Could not load the schedule — retrying…' : 'Loading…'; u.className = lastError ? 'updated stale' : 'updated'; return; }
  const t = lastGoodAt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  u.textContent = lastError ? `Last updated ${t} — can't reach the server, retrying` : `Last updated ${t}`;
  u.className = lastError ? 'updated stale' : 'updated';
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function showBanner(msg, kind) {
  const b = $('banner');
  b.textContent = msg;
  b.className = `banner ${kind || ''}`;
  b.hidden = false;
  b.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function safeGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

// ---------------------------------------------------------------- booking ---

function openBooking(date, cls, slot) {
  pending = { date, cls, slot: slot.i, start: slot.start };
  $('book-title').textContent = `Book ${cls}${slot.i} · ${prettyDate(date)} · ${prettyTime(slot.start)}`;
  const sel = $('book-name');
  sel.textContent = '';
  const people = (schedule.roster || []).filter((s) => s.class === cls)
    .sort((a, b) => a.displayName.localeCompare(b.displayName));
  sel.appendChild(new Option('Choose your name…', ''));
  const myName = safeGet(LS_NAME);
  for (const p of people) sel.appendChild(new Option(p.displayName, p.rosterId, false, p.displayName === myName));
  $('book-code').value = safeGet(LS_CODE);
  setBookError('');
  $('book-submit').disabled = false;
  $('book-dialog').showModal();
}

function setBookError(msg) {
  const e = $('book-error');
  e.textContent = msg;
  e.hidden = !msg;
}

async function submitBooking(ev) {
  ev.preventDefault();
  const rosterId = $('book-name').value;
  const classCode = $('book-code').value.trim();
  if (!rosterId) return setBookError('Pick your name.');
  if (!classCode) return setBookError('Enter the class code posted in the room.');
  $('book-submit').disabled = true;
  setBookError('');
  const res = await api('POST', { action: 'book', rosterId, date: pending.date, class: pending.cls, slot: pending.slot, classCode });
  $('book-submit').disabled = false;
  if (!res.ok) {
    setBookError(BOOK_ERRORS[res.error] || BOOK_ERRORS.unreachable);
    if (res.error === 'slot_taken' || res.error === 'blocked' || res.error === 'past') refresh();
    return;
  }
  safeSet(LS_CODE, classCode);
  safeSet(LS_NAME, res.displayName || $('book-name').selectedOptions[0].textContent);
  $('book-dialog').close();
  const what = `${pending.cls}${pending.slot} · ${prettyDate(pending.date)} · ${prettyTime(pending.start)}`;
  if (res.emailSent === false) {
    showBanner(`Booked ${what}, but the email didn't send — ask your teacher if you need to cancel.`, 'warn');
  } else {
    showBanner(`Booked ${what}. Check your email for the confirmation (it has your cancel link).`, '');
  }
  refresh();
}

// ----------------------------------------------------------------- cancel ---

async function startCancelFlow(token) {
  const panel = $('cancel-panel');
  panel.hidden = false;
  panel.className = 'panel';
  panel.textContent = 'Looking up your booking…';
  const res = await api('POST', { action: 'cancel', token, confirm: false });
  if (!res.ok) return showCancelError(res);
  const b = res.booking;
  panel.textContent = '';
  panel.appendChild(el('p', '', `Cancel ${b.displayName}'s booking: ${b.class}${b.slot} · ${prettyDate(b.date)} · ${prettyTime(b.start)}?`));
  const actions = el('div', 'actions');
  const keep = el('button', 'secondary', 'Keep it');
  const go = el('button', '', 'Confirm cancel');
  keep.type = go.type = 'button';
  keep.addEventListener('click', () => { panel.hidden = true; clearCancelParam(); });
  go.addEventListener('click', async () => {
    go.disabled = keep.disabled = true;
    const r = await api('POST', { action: 'cancel', token, confirm: true });
    if (!r.ok) return showCancelError(r);
    panel.className = 'panel';
    panel.textContent = 'Cancelled — the slot is open again.' + (r.emailSent === false ? '' : ' A confirmation email is on its way.');
    clearCancelParam();
    refresh();
  });
  actions.append(keep, go);
  panel.appendChild(actions);
}

function showCancelError(res) {
  const panel = $('cancel-panel');
  panel.className = 'panel error';
  panel.textContent = CANCEL_ERRORS[res.error] || CANCEL_ERRORS.unreachable;
  if (res.error === 'busy' || res.error === 'unreachable') {
    const retry = el('button', 'secondary', 'Try again');
    retry.type = 'button';
    retry.style.marginLeft = '8px';
    retry.addEventListener('click', () => startCancelFlow(new URLSearchParams(location.search).get('cancel')));
    panel.appendChild(retry);
  }
}

function clearCancelParam() {
  try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------------- boot ---

function boot() {
  $('book-form').addEventListener('submit', submitBooking);
  $('book-cancel').addEventListener('click', () => $('book-dialog').close());
  if (!configured()) { render(); return; }

  const token = new URLSearchParams(location.search).get('cancel');
  if (token) startCancelFlow(token);

  refresh().then(scheduleNextPoll);                    // one fetch on load (includes the roster)
  let lastFocusFetch = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || Date.now() - lastFocusFetch < 5000) return;
    lastFocusFetch = Date.now();
    refresh().then(scheduleNextPoll);
  });
  setInterval(() => { if (schedule && !$('book-dialog').open) render(); }, 30000);  // roll Open→Now→Past
}

boot();
