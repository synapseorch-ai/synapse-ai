#!/usr/bin/env node
/*
 * `synapse` from npm: a thin launcher over the same install the script makes.
 *
 * The first run — and the first after `npm install -g synapse-orch-ai@<new>` —
 * runs the installer this package carries (installer/synapse-install.pyz), which
 * shows its steps, asks its questions and installs the matching Synapse with uv
 * into ~/.synapse. Every run after that hands the command to that Synapse's own
 * CLI, so npm users get the same screens, the same data folder and the same
 * commands as everyone else.
 *
 * It used to be a second implementation of the whole CLI: its own venv, its own
 * setup wizard, and a database inside the npm package folder, which
 * `npm i -g synapse-orch-ai@latest` deleted.
 */
'use strict';

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const pkg = require('../package.json');
const IS_WIN = process.platform === 'win32';
const HOME = process.env.SYNAPSE_HOME || path.join(os.homedir(), '.synapse');
const PYZ = process.env.SYNAPSE_INSTALLER || path.join(__dirname, '..', 'installer', 'synapse-install.pyz');
const UV_VERSION = '0.11.8';

function readInstall() {
  try {
    return JSON.parse(fs.readFileSync(path.join(HOME, 'install.json'), 'utf8'));
  } catch {
    return {};
  }
}

function onPath(name) {
  const out = spawnSync(IS_WIN ? 'where' : 'which', [name], { encoding: 'utf8' });
  return out.status === 0 ? out.stdout.split(/\r?\n/)[0].trim() : '';
}

function log(line) {
  fs.mkdirSync(path.join(HOME, 'logs'), { recursive: true });
  fs.appendFileSync(path.join(HOME, 'logs', 'install.log'), line + '\n');
}

/** uv: one already here, or a private copy in ~/.synapse/runtime that touches no PATH. */
function findUv() {
  const privateUv = path.join(HOME, 'runtime', 'uv', IS_WIN ? 'uv.exe' : 'uv');
  for (const candidate of [process.env.SYNAPSE_UV, onPath('uv'), privateUv]) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  process.stdout.write('\n  Getting the Synapse installer…');
  const env = { ...process.env, UV_UNMANAGED_INSTALL: path.join(HOME, 'runtime', 'uv') };
  const out = IS_WIN
    ? spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
        `irm https://astral.sh/uv/${UV_VERSION}/install.ps1 | iex`], { env, encoding: 'utf8' })
    : spawnSync('sh', ['-c', `curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh`],
        { env, encoding: 'utf8' });
  log(`uv install: ${out.stdout || ''}${out.stderr || ''}`);
  if (!fs.existsSync(privateUv)) {
    console.error(`\n\n  Could not install uv (details in ${path.join(HOME, 'logs', 'install.log')}).\n`);
    process.exit(1);
  }
  return privateUv;
}

/** The Synapse this package's version names, installed if it is not already. */
function ensureInstalled() {
  const record = readInstall();
  if (record.version === pkg.version && record.tool_python && fs.existsSync(record.tool_python)) {
    return record;
  }
  const uv = findUv();
  const args = ['run', '--no-project', '--quiet', '--python', '3.12', 'python', PYZ,
                '--method', 'npm', '--no-start', '--uv', uv, '--version', pkg.version];
  const out = spawnSync(uv, args, { stdio: 'inherit' });
  if (out.status !== 0) process.exit(out.status || 1);
  const installed = readInstall();
  if (!installed.tool_python) {
    console.error('\n  The install finished without recording where Synapse is. Run `synapse` again.\n');
    process.exit(1);
  }
  return installed;
}

const cliArg = process.argv[2];

if (cliArg === '--version' || cliArg === '-V') {
  // What SECURITY.md asks a reporter to run; must answer without installing (F80).
  console.log(`synapse ${pkg.version}`);
  process.exit(0);
}

const record = ensureInstalled();
const child = spawn(record.tool_python, ['-m', 'synapse', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: { ...process.env, SYNAPSE_LAUNCHER: 'npm', SYNAPSE_HOME: HOME },
});
for (const signal of ['SIGINT', 'SIGTERM']) {
  // The CLI handles Ctrl+C itself (it stops the servers); only pass it on.
  process.on(signal, () => child.kill(signal));
}
child.on('exit', (code, signal) => process.exit(signal ? 1 : (code ?? 1)));
