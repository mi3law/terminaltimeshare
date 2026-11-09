#!/usr/bin/env node
// One-command backend deploy. Pushes backend/Code.gs to the Apps Script project and moves the
// existing web app deployment to a new version: the same /exec URL, so the booking page and the
// kiosk need nothing. It replaces "paste Code.gs, then Deploy → Manage deployments → ✏️ → New version".
//
//   node backend/deploy.js            deploy (from main, with backend/Code.gs committed)
//   node backend/deploy.js --dry-run  show what would be pushed; changes nothing
//
// One-time setup (clasp, the Apps Script API switch, backend/script-id.txt): README §8.
//
// Your settings at the top of the live Code.gs (SCHOOL_TZ, SPA_URL, MAIL_NAME) are read from the
// deployed copy and carried into the pushed one, so they never have to be in git (SPEC §3).
// It does not run functions: when a change adds Config columns or tabs, still run setup() once.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync, spawnSync } = require('child_process');

const SETTINGS = ['SCHOOL_TZ', 'SPA_URL', 'MAIL_NAME'];
const HERE = __dirname;
const REPO = path.resolve(HERE, '..');
const ID_FILE = path.join(HERE, 'script-id.txt');

/** The whole `var NAME = …` line for a setting, as written in `text`; null when absent. */
function settingLine(text, name) {
  const m = text.match(new RegExp('^var ' + name + '\\s*=[^\\n]*$', 'm'));
  return m ? m[0] : null;
}

/** repoText with each setting line swapped for the live copy's. Settings the live copy lacks keep
 *  the repo default and are listed in `missing`. */
function carrySettings(repoText, liveText) {
  let text = repoText;
  const carried = [];
  const missing = [];
  for (const name of SETTINGS) {
    const mine = settingLine(repoText, name);
    if (!mine) throw new Error(`backend/Code.gs has no "var ${name} = …" line; deploy.js expects one.`);
    const live = settingLine(liveText, name);
    if (!live) { missing.push(name); continue; }
    text = text.replace(mine, () => live);        // a function, so "$&" etc. in a value stay literal
    carried.push(live);
  }
  return { text, carried, missing };
}

/** The deployment id inside app.js's EXEC_URL: the deployment the booking page calls. */
function deploymentId(appJs) {
  const m = appJs.match(/script\.google\.com\/macros\/s\/([A-Za-z0-9_-]+)\/exec/);
  if (!m || /PASTE/i.test(m[1])) throw new Error('app.js has no EXEC_URL deployment id yet (README §4).');
  return m[1];
}

// ---------------------------------------------------------------------------------------------

const read = (p) => fs.readFileSync(p, 'utf8');
const git = (...args) => execFileSync('git', args, { cwd: REPO, encoding: 'utf8' }).trim();
const run = (cmd, args, cwd) => execFileSync(cmd, args, { cwd, stdio: ['ignore', 'inherit', 'inherit'] });

async function main() {
  const dry = process.argv.includes('--dry-run');

  if (!fs.existsSync(ID_FILE)) throw new Error('No backend/script-id.txt yet. One-time setup: README §8.');
  const scriptId = read(ID_FILE).trim();
  if (!/^[A-Za-z0-9_-]{20,}$/.test(scriptId)) throw new Error('backend/script-id.txt should hold just the Script ID.');
  const appJs = read(path.join(REPO, 'app.js'));
  const depId = deploymentId(appJs);
  const execUrl = appJs.match(/https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec/)[0];
  try {
    execFileSync('clasp', ['--version'], { stdio: 'ignore' });
  } catch (e) {
    throw new Error('clasp is not installed. Run: npm install -g @google/clasp@3   (README §8)');
  }

  // Deploy only committed code from main, so every live version is a commit the booking page also runs.
  const branch = git('rev-parse', '--abbrev-ref', 'HEAD');
  const dirty = git('status', '--porcelain', '--', 'backend/Code.gs');
  const sha = git('rev-parse', '--short', 'HEAD');
  if (!dry && branch !== 'main') throw new Error(`On branch "${branch}": merge into main and deploy from there.`);
  if (!dry && dirty) throw new Error('backend/Code.gs has uncommitted changes: commit them first.');

  for (const t of ['mock_test.js', 'deploy_test.js']) {
    const r = spawnSync(process.execPath, [path.join(HERE, 'test', t)], { encoding: 'utf8' });
    if (r.status !== 0) {
      process.stderr.write(r.stdout + r.stderr);
      throw new Error(`backend/test/${t} failed: nothing was deployed.`);
    }
    console.log(r.stdout.trim().split('\n').pop());     // "N … checks passed"
  }

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'timeshare-deploy-'));
  const proj = path.join(tmp, 'project');                 // what clasp pushes; nothing else goes in here
  fs.mkdirSync(proj);
  try {
    fs.writeFileSync(path.join(proj, '.clasp.json'), JSON.stringify({ scriptId }));
    console.log('\nFetching the live Apps Script project…');
    run('clasp', ['pull'], proj);

    const files = fs.readdirSync(proj).filter((f) => !f.startsWith('.'));
    const code = files.filter((f) => /\.(gs|js)$/.test(f) && settingLine(read(path.join(proj, f)), 'SPA_URL'));
    if (code.length !== 1) {
      throw new Error(`Expected one live file with "var SPA_URL", found ${code.length} among: ${files.join(', ')}.`);
    }
    const livePath = path.join(proj, code[0]);
    const liveText = read(livePath);
    const { text, carried, missing } = carrySettings(read(path.join(HERE, 'Code.gs')), liveText);

    console.log('\nKeeping your settings from the live copy:');
    carried.forEach((l) => console.log('  ' + l));
    missing.forEach((n) => console.log(`  ${n}: not in the live copy, using the repo default`));
    if (/YOUR-GITHUB-USERNAME/.test(settingLine(text, 'SPA_URL'))) {
      console.log('  WARNING: SPA_URL is still the placeholder, so cancel links in emails are broken.');
    }
    const others = files.filter((f) => f !== code[0]);
    console.log(`Pushed back unchanged: ${others.join(', ') || '(nothing else)'}`);

    fs.writeFileSync(path.join(tmp, 'live-before.gs'), liveText);
    fs.writeFileSync(livePath, text);
    if (text === liveText) {
      console.log('\nThe live code already matches; deploying anyway, in case it was saved but never deployed.');
    } else {
      console.log('\nChange from the live code:');
      try { git('diff', '--no-index', '--stat', path.join(tmp, 'live-before.gs'), livePath); } catch (e) {
        console.log(String(e.stdout || '').trim());   // diff exits 1 when the files differ
      }
    }

    if (dry) {
      console.log('\nDry run: clasp would push these files:');
      run('clasp', ['show-file-status'], proj);
      console.log('Nothing was pushed or deployed.');
      return;
    }

    console.log('\nPushing…');
    run('clasp', ['push', '--force'], proj);
    console.log('Moving the web app deployment to a new version (same URL)…');
    run('clasp', ['update-deployment', depId, '--description', `git ${sha}`], proj);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }

  const res = await fetch(execUrl + '?action=schedule');
  const body = await res.json().catch(() => null);
  if (!body || !body.ok) throw new Error('Deployed, but the web app did not answer ok: ' + JSON.stringify(body));
  const blocks = ((body.days[0] && body.days[0].classes) || []).map((c) => c.label || c.class).join(' / ');
  console.log(`\nLive (git ${sha}): ${body.roster.length} students on the roster; blocks ${blocks}.`);
  if (!body.roster.length) console.log('No students show: in the Sheet, run Timeshare → Check roster.');
}

if (require.main === module) {
  main().catch((err) => {
    console.error('\ndeploy: ' + (err && err.message || err));
    process.exitCode = 1;
  });
} else {
  module.exports = { settingLine, carrySettings, deploymentId, SETTINGS };
}
