// Offline checks for backend/deploy.js: the settings carried over from the live Code.gs, and the
// deployment id read from app.js. Nothing here talks to Google.
// Run:  node backend/test/deploy_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { settingLine, carrySettings, deploymentId, SETTINGS } = require('../deploy.js');

let pass = 0;
function check(name, fn) { fn(); pass++; console.log('ok  ' + name); }

const repoCode = fs.readFileSync(path.join(__dirname, '..', 'Code.gs'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '..', '..', 'app.js'), 'utf8');

const sample = [
  "var SCHOOL_TZ = 'Asia/Riyadh';",
  "var SPA_URL = 'https://YOUR-GITHUB-USERNAME.github.io/terminaltimeshare/';",
  "var MAIL_NAME = 'AI Terminal';",
  'var SLOT_MINUTES = 15;',
].join('\n');

check('the real Code.gs has exactly one line per setting', () => {
  for (const name of SETTINGS) {
    const lines = repoCode.split('\n').filter((l) => new RegExp('^var ' + name + '\\s*=').test(l));
    assert.strictEqual(lines.length, 1, name);
  }
});

check('live settings replace the repo defaults; the rest of the file is the repo copy', () => {
  const live = [
    "var SCHOOL_TZ = 'Asia/Kuwait';",
    "var SPA_URL = 'https://someone.github.io/terminaltimeshare/';   // mine",
    "var MAIL_NAME = 'Room 9 $& $1 Terminal';",
    'var SLOT_MINUTES = 99;',
  ].join('\n');
  const r = carrySettings(sample, live);
  assert.deepStrictEqual(r.text.split('\n'), [
    "var SCHOOL_TZ = 'Asia/Kuwait';",
    "var SPA_URL = 'https://someone.github.io/terminaltimeshare/';   // mine",
    "var MAIL_NAME = 'Room 9 $& $1 Terminal';",
    'var SLOT_MINUTES = 15;',
  ]);
  assert.deepStrictEqual(r.missing, []);
  assert.strictEqual(r.carried.length, 3);
});

check('a setting the live copy lacks keeps the repo default', () => {
  const r = carrySettings(sample, "var SCHOOL_TZ = 'Asia/Qatar';\nvar SPA_URL = 'https://x.github.io/t/';");
  assert.deepStrictEqual(r.missing, ['MAIL_NAME']);
  assert.strictEqual(settingLine(r.text, 'MAIL_NAME'), "var MAIL_NAME = 'AI Terminal';");
  assert.strictEqual(settingLine(r.text, 'SCHOOL_TZ'), "var SCHOOL_TZ = 'Asia/Qatar';");
});

check('a mention inside a comment or a longer name is not a setting line', () => {
  const text = "// var SPA_URL = 'nope';\nvar SPA_URL_OLD = 'nope';\nvar SPA_URL = 'yes';";
  assert.strictEqual(settingLine(text, 'SPA_URL'), "var SPA_URL = 'yes';");
});

check('carrying the repo copy onto itself changes nothing', () => {
  assert.strictEqual(carrySettings(repoCode, repoCode).text, repoCode);
});

check('a repo Code.gs without a setting line is refused', () => {
  assert.throws(() => carrySettings('var SCHOOL_TZ = 1;\nvar SPA_URL = 2;', sample), /MAIL_NAME/);
});

check('deployment id comes from EXEC_URL; the README placeholder is refused', () => {
  assert.strictEqual(deploymentId("const EXEC_URL = 'https://script.google.com/macros/s/AKfy_abc-123/exec';"), 'AKfy_abc-123');
  assert.throws(() => deploymentId("'https://script.google.com/macros/s/PASTE-YOUR-DEPLOYMENT-ID/exec'"), /EXEC_URL/);
  assert.throws(() => deploymentId('no url here'), /EXEC_URL/);
  assert.match(deploymentId(appJs), /^[A-Za-z0-9_-]{20,}$/);
});

console.log(`\n${pass} deploy checks passed`);
