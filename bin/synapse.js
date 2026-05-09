#!/usr/bin/env node
'use strict';

const { spawnSync, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const http = require('http');
const https = require('https');
const os = require('os');
const readline = require('readline');

const PKG_DIR = path.resolve(__dirname, '..');
const BACKEND_DIR = path.join(PKG_DIR, 'backend');
const FRONTEND_BUILD = path.join(PKG_DIR, 'frontend-build');
const REQUIREMENTS = path.join(BACKEND_DIR, 'requirements.txt');

const SYNAPSE_HOME = path.join(os.homedir(), '.synapse');
const VENV_DIR = path.join(SYNAPSE_HOME, 'venv');
const DATA_DIR = process.env.SYNAPSE_DATA_DIR || path.join(SYNAPSE_HOME, 'data');
const HASH_FILE = path.join(SYNAPSE_HOME, 'requirements.hash');
const SETTINGS_FILE = path.join(DATA_DIR, 'settings.json');

let BACKEND_PORT = parseInt(process.env.SYNAPSE_BACKEND_PORT || '8765');
let FRONTEND_PORT = parseInt(process.env.SYNAPSE_FRONTEND_PORT || '3000');

const IS_WIN = os.platform() === 'win32';

// ── Python executable detection ───────────────────────────────────────────────

function pythonCmd() {
  if (!IS_WIN) return 'python3';
  const r = spawnSync('python', ['--version'], { stdio: 'pipe', shell: true });
  if (r.status === 0) return 'python';
  return 'python3';
}

const PYTHON = pythonCmd();

// ── Prerequisite checks ───────────────────────────────────────────────────────

function checkCmd(cmd) {
  // On Windows use shell:true but pass a single string to avoid DEP0190
  const result = IS_WIN
    ? spawnSync(`${cmd} --version`, { stdio: 'pipe', shell: true })
    : spawnSync(cmd, ['--version'], { stdio: 'pipe' });
  return result.status === 0;
}

function checkPrerequisites() {
  if (!checkCmd(PYTHON)) {
    console.error('Error: python3 not found. Install Python 3.11+ from https://www.python.org/');
    process.exit(1);
  }
  const result = spawnSync(PYTHON, ['-c', 'import sys; print(sys.version_info[:2])'], { stdio: 'pipe' });
  if (result.status === 0) {
    const out = result.stdout.toString().trim();
    const match = out.match(/\((\d+),\s*(\d+)\)/);
    if (match) {
      const [, major, minor] = match.map(Number);
      if (major < 3 || (major === 3 && minor < 11)) {
        console.error(`Error: Python 3.11+ required, found ${major}.${minor}. Install from https://www.python.org/`);
        process.exit(1);
      }
    }
  }
  if (!checkCmd('npx')) {
    console.error('Error: npx not found. Install Node.js from https://nodejs.org/');
    process.exit(1);
  }
}

// ── Progress bar for pip install ──────────────────────────────────────────────

function countRequirements(reqFile) {
  if (!fs.existsSync(reqFile)) return 0;
  return fs.readFileSync(reqFile, 'utf8')
    .split('\n')
    .filter(l => l.trim() && !l.trim().startsWith('#'))
    .length;
}

function renderBar(done, total) {
  const width = 28;
  const pct = total > 0 ? Math.min(done / total, 1) : 0;
  const filled = Math.round(pct * width);
  const bar = '█'.repeat(filled) + '░'.repeat(width - filled);
  const pctStr = Math.round(pct * 100).toString().padStart(3);
  return `  [${bar}] ${pctStr}% (${done}/${total})`;
}

// Spinner shown while pip goes silent after downloading (build/install phase)
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

function installPipDeps(reqFile) {
  return new Promise((resolve) => {
    const total = countRequirements(reqFile);
    let done = 0;
    let lineBuffer = '';
    let spinnerActive = false;
    let spinnerFrame = 0;
    let spinnerTimer = null;
    let staleTimer = null;

    // How long (ms) of silence before we switch to the "building" spinner
    const STALE_THRESHOLD = 3000;

    process.stdout.write(renderBar(0, total) + '\r');

    function stopSpinner() {
      if (!spinnerActive) return;
      spinnerActive = false;
      clearInterval(spinnerTimer);
      spinnerTimer = null;
    }

    function startSpinner() {
      if (spinnerActive) return;
      spinnerActive = true;
      spinnerFrame = 0;
      spinnerTimer = setInterval(() => {
        const frame = SPINNER_FRAMES[spinnerFrame % SPINNER_FRAMES.length];
        spinnerFrame++;
        process.stdout.write(`  ${frame} Building & installing packages — please stay put...\r`);
      }, 100);
    }

    function resetStaleTimer() {
      if (staleTimer) clearTimeout(staleTimer);
      staleTimer = setTimeout(() => {
        // Pip has gone quiet — it's in the build/install phase now
        startSpinner();
      }, STALE_THRESHOLD);
    }

    const pip = spawn(venvPip(), ['install', '-r', reqFile, '--progress-bar', 'off'], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    // Kick off the stale-detection timer right away
    resetStaleTimer();

    function parseLines(chunk) {
      lineBuffer += chunk.toString();
      const lines = lineBuffer.split('\n');
      lineBuffer = lines.pop() || '';
      for (const line of lines) {
        if (/^(Collecting|Using cached) /.test(line)) {
          // New output means pip is still downloading — stop any spinner
          stopSpinner();
          // Cap at total-1 so the bar only hits 100% when pip actually exits
          done = Math.min(done + 1, total > 1 ? total - 1 : done + 1);
          process.stdout.write(renderBar(done, total) + '\r');
          // Reset silence timer so spinner only fires if pip goes quiet again
          resetStaleTimer();
        }
      }
    }

    pip.stdout.on('data', parseLines);
    pip.stderr.on('data', parseLines);

    pip.on('close', (code) => {
      if (staleTimer) clearTimeout(staleTimer);
      stopSpinner();
      // Clear the current line before printing final bar
      process.stdout.write('\r' + ' '.repeat(70) + '\r');
      process.stdout.write(renderBar(total, total) + '\n');
      if (code !== 0) {
        console.error('\n  Failed to install Python dependencies.');
        process.exit(1);
      }
      console.log('  ✓ Python dependencies installed.');
      resolve();
    });
  });
}

// ── Python venv setup ─────────────────────────────────────────────────────────

function getRequirementsHash() {
  if (!fs.existsSync(REQUIREMENTS)) return null;
  return crypto.createHash('md5').update(fs.readFileSync(REQUIREMENTS)).digest('hex');
}

function venvPython() {
  return IS_WIN
    ? path.join(VENV_DIR, 'Scripts', 'python.exe')
    : path.join(VENV_DIR, 'bin', 'python');
}

function venvPip() {
  return IS_WIN
    ? path.join(VENV_DIR, 'Scripts', 'pip.exe')
    : path.join(VENV_DIR, 'bin', 'pip');
}

async function setupVenv() {
  const currentHash = getRequirementsHash();
  const savedHash = fs.existsSync(HASH_FILE) ? fs.readFileSync(HASH_FILE, 'utf8').trim() : null;

  if (fs.existsSync(venvPython()) && currentHash === savedHash) {
    return;
  }

  if (!fs.existsSync(VENV_DIR)) {
    process.stdout.write('Creating Python virtual environment...');
    const result = spawnSync(PYTHON, ['-m', 'venv', VENV_DIR], { stdio: ['ignore', 'pipe', 'pipe'] });
    if (result.status !== 0) {
      console.error('\nFailed to create virtual environment.');
      process.exit(1);
    }
    console.log(' done.');
  }

  console.log('Installing Python dependencies...');
  await installPipDeps(REQUIREMENTS);

  if (currentHash) fs.writeFileSync(HASH_FILE, currentHash);
}

async function installPlaywrightBrowsers() {
  const browsersPath = IS_WIN
    ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'ms-playwright')
    : os.platform() === 'darwin'
      ? path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright')
      : path.join(os.homedir(), '.cache', 'ms-playwright');

  try {
    if (fs.existsSync(browsersPath)) {
      const dirs = fs.readdirSync(browsersPath);
      if (dirs.some(d => d.startsWith('chromium-'))) {
        return;
      }
    }
  } catch (e) {
    // Ignore error reading dir
  }

  process.stdout.write('Installing Playwright browsers...');
  try {
    const res1 = spawnSync(venvPython(), ['-m', 'playwright', 'install', 'chromium'], { stdio: 'pipe' });
    if (res1.status !== 0) throw new Error('Failed to install python playwright');

    const env = { ...process.env, PLAYWRIGHT_BROWSERS_PATH: browsersPath };
    const npxCmd = IS_WIN ? 'npx.cmd' : 'npx';
    const res2 = spawnSync(npxCmd, ['-y', '@playwright/mcp', 'install-browser', 'chromium'], { env, stdio: 'pipe', shell: IS_WIN });
    if (res2.status !== 0) throw new Error('Failed to install mcp playwright');

    console.log(' done.');
  } catch (e) {
    console.log(`\n  Warning: Failed to install Playwright browsers: ${e.message}`);
    return;
  }

  try {
    if (fs.existsSync(SETTINGS_FILE)) {
      const settings = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8'));
      settings.playwright_browsers_path = browsersPath;
      fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 4));
    }
  } catch (e) {
    console.log(`\n  Warning: Failed to save playwright_browsers_path to settings: ${e.message}`);
  }
}

// ── Data directory ────────────────────────────────────────────────────────────

const DEFAULT_JSON = {
  'user_agents.json': '[]',
  'orchestrations.json': '[]',
  'repos.json': '[]',
  'mcp_servers.json': '[]',
  'custom_tools.json': '[]',
  'db_configs.json': '[]',
};

function ensureDataDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  for (const sub of ['vault', 'datasets', 'orchestration_runs', 'orchestration_logs']) {
    fs.mkdirSync(path.join(DATA_DIR, sub), { recursive: true });
  }
  for (const [file, content] of Object.entries(DEFAULT_JSON)) {
    const target = path.join(DATA_DIR, file);
    if (!fs.existsSync(target)) fs.writeFileSync(target, content);
  }
}

// ── Model fetching ────────────────────────────────────────────────────────────

function fetchJson(url, headers = {}) {
  return new Promise((resolve) => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { headers }, (res) => {
      let body = '';
      res.on('data', (c) => body += c);
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch { resolve(null); } });
    });
    req.setTimeout(10000, () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
  });
}

async function fetchOllamaModels(baseUrl = 'http://127.0.0.1:11434') {
  const data = await fetchJson(`${baseUrl}/api/tags`);
  return (data?.models || []).map(m => m.name).filter(Boolean);
}

async function fetchGeminiModels(key) {
  const data = await fetchJson(
    `https://generativelanguage.googleapis.com/v1beta/models?key=${key}`
  );
  return (data?.models || [])
    .filter(m => m.name?.startsWith('models/') && m.supportedGenerationMethods?.includes('generateContent'))
    .map(m => m.name.replace('models/', ''))
    .sort();
}

async function fetchOpenAIModels(key) {
  const data = await fetchJson('https://api.openai.com/v1/models', { Authorization: `Bearer ${key}` });
  return [...new Set((data?.data || []).map(m => m.id).filter(id => id && /gpt-4|gpt-3\.5|o1|o3/.test(id)))].sort();
}

async function fetchAnthropicModels(key) {
  const data = await fetchJson('https://api.anthropic.com/v1/models', {
    'x-api-key': key,
    'anthropic-version': '2023-06-01',
  });
  return [...new Set((data?.data || []).map(m => m.id).filter(Boolean))].sort().reverse();
}

// ── Setup Wizard ──────────────────────────────────────────────────────────────

function ask(rl, question) {
  return new Promise((resolve) => rl.question(question, (ans) => resolve(ans.trim())));
}

async function askDefault(rl, label, defaultVal) {
  const ans = await ask(rl, `${label} [${defaultVal}]: `);
  return ans || String(defaultVal);
}

async function askChoice(rl, label, options) {
  console.log('\n' + label);
  options.forEach((o, i) => console.log(`  ${i + 1}. ${o}`));
  while (true) {
    const ans = await ask(rl, 'Select: ');
    const n = parseInt(ans);
    if (!isNaN(n) && n >= 1 && n <= options.length) return options[n - 1];
    console.log(`  Enter a number 1-${options.length}.`);
  }
}

async function runSetupWizard(ollamaAvailable) {
  console.log('\n+----------------------------------------------------------+');
  console.log('|          Synapse -- First-Time Setup                     |');
  console.log('+----------------------------------------------------------+\n');

  if (!ollamaAvailable) {
    console.log('  [!] Ollama is not installed -- local models are unavailable.');
    console.log('      You will need a cloud API key (OpenAI, Anthropic, Gemini, etc.).\n');
  }

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  try {
    console.log('General');
    const agentName = await askDefault(rl, 'Agent name', 'Synapse');

    const providers = ollamaAvailable
      ? ['Ollama (local)', 'OpenAI', 'Claude (Anthropic)', 'Gemini', 'OpenAI Compatible', 'Local V1 Compatible', 'Bedrock (AWS)', 'Skip for now']
      : ['OpenAI', 'Claude (Anthropic)', 'Gemini', 'OpenAI Compatible', 'Local V1 Compatible', 'Bedrock (AWS)', 'Skip for now'];

    const provider = await askChoice(rl, 'LLM Provider:', providers);

    const cfg = {
      agent_name: agentName,
      model: '',
      mode: 'cloud',
      openai_key: '',
      anthropic_key: '',
      gemini_key: '',
      ollama_base_url: '',
      openai_compatible_key: '',
      openai_compatible_base_url: '',
      openai_compatible_models: '',
      local_compatible_base_url: '',
      local_compatible_key: '',
      local_compatible_models: '',
      bedrock_api_key: '',
      bedrock_inference_profile: '',
      aws_region: 'us-east-1',
      coding_agent_enabled: true,
      report_agent_enabled: true,
      backend_port: BACKEND_PORT,
      frontend_port: FRONTEND_PORT,
    };

    if (provider.startsWith('Ollama')) {
      cfg.mode = 'local';
      cfg.ollama_base_url = await askDefault(rl, 'Ollama base URL', 'http://127.0.0.1:11434');
      process.stdout.write('  Fetching Ollama models...');
      const ollamaModels = await fetchOllamaModels(cfg.ollama_base_url);
      console.log('');
      if (ollamaModels.length > 0) {
        cfg.model = await askChoice(rl, 'Select model', ollamaModels);
      } else {
        console.log('  No models found at that URL.');
        cfg.model = await askDefault(rl, 'Model name (e.g. mistral, llama3)', 'mistral');
      }
    } else if (provider === 'OpenAI') {
      cfg.openai_key = await ask(rl, 'OpenAI API key: ');
      process.stdout.write('  Fetching available models...');
      const openaiModels = await fetchOpenAIModels(cfg.openai_key);
      console.log('');
      if (openaiModels.length > 0) {
        cfg.model = await askChoice(rl, 'Select model', openaiModels);
      } else {
        console.log('  Could not fetch models. Check your key.');
        cfg.model = await askDefault(rl, 'Model name', 'gpt-4o');
      }
    } else if (provider === 'Claude (Anthropic)') {
      cfg.anthropic_key = await ask(rl, 'Anthropic API key: ');
      process.stdout.write('  Fetching available models...');
      const claudeModels = await fetchAnthropicModels(cfg.anthropic_key);
      console.log('');
      if (claudeModels.length > 0) {
        cfg.model = await askChoice(rl, 'Select model', claudeModels);
      } else {
        console.log('  Could not fetch models. Check your key.');
        cfg.model = await askDefault(rl, 'Model name', 'claude-sonnet-4-6');
      }
    } else if (provider === 'Gemini') {
      cfg.gemini_key = await ask(rl, 'Gemini API key: ');
      process.stdout.write('  Fetching available models...');
      const geminiModels = await fetchGeminiModels(cfg.gemini_key);
      console.log('');
      if (geminiModels.length > 0) {
        cfg.model = await askChoice(rl, 'Select model', geminiModels);
      } else {
        console.log('  Could not fetch models. Check your key.');
        cfg.model = await askDefault(rl, 'Model name', 'gemini-2.0-flash');
      }
    } else if (provider === 'OpenAI Compatible') {
      cfg.openai_compatible_key = await askDefault(rl, 'API key', '');
      cfg.openai_compatible_base_url = await askDefault(rl, 'Base URL (without /v1)', '');
      if (cfg.openai_compatible_base_url) {
        process.stdout.write('  Fetching available models...');
        const d = await fetchJson(
          cfg.openai_compatible_base_url.replace(/\/$/, '') + '/v1/models',
          { Authorization: `Bearer ${cfg.openai_compatible_key}` }
        );
        const models = (d?.data || []).map(m => m.id).filter(Boolean);
        console.log('');
        if (models.length > 0) {
          const chosen = await askChoice(rl, 'Select model', models);
          cfg.openai_compatible_models = chosen;
          cfg.model = `oaic.${chosen}`;
        } else {
          cfg.openai_compatible_models = await askDefault(rl, 'Model names (comma-separated)', '');
          if (cfg.openai_compatible_models) cfg.model = 'oaic.' + cfg.openai_compatible_models.split(',')[0].trim();
        }
      }
    } else if (provider === 'Local V1 Compatible') {
      cfg.local_compatible_base_url = await askDefault(rl, 'Base URL (without /v1)', '');
      cfg.local_compatible_key = await askDefault(rl, 'API key (optional)', '');
      if (cfg.local_compatible_base_url) {
        process.stdout.write('  Fetching available models...');
        const hdrs = cfg.local_compatible_key ? { Authorization: `Bearer ${cfg.local_compatible_key}` } : {};
        const d = await fetchJson(cfg.local_compatible_base_url.replace(/\/$/, '') + '/v1/models', hdrs);
        const models = (d?.data || []).map(m => m.id).filter(Boolean);
        console.log('');
        if (models.length > 0) {
          const chosen = await askChoice(rl, 'Select model', models);
          cfg.local_compatible_models = chosen;
          cfg.model = `locv1.${chosen}`;
        } else {
          cfg.local_compatible_models = await askDefault(rl, 'Model names (comma-separated)', '');
          if (cfg.local_compatible_models) cfg.model = 'locv1.' + cfg.local_compatible_models.split(',')[0].trim();
        }
      }
    } else if (provider === 'Bedrock (AWS)') {
      cfg.bedrock_api_key = await askDefault(rl, 'Bedrock API key', '');
      cfg.aws_region = await askDefault(rl, 'AWS region', 'us-east-1');
    }

    console.log('\nPorts (press Enter to keep defaults):');
    const backendStr = await askDefault(rl, 'Backend port', String(BACKEND_PORT));
    const frontendStr = await askDefault(rl, 'Frontend (UI) port', String(FRONTEND_PORT));
    cfg.backend_port = parseInt(backendStr) || BACKEND_PORT;
    cfg.frontend_port = parseInt(frontendStr) || FRONTEND_PORT;
    BACKEND_PORT = cfg.backend_port;
    FRONTEND_PORT = cfg.frontend_port;

    rl.close();

    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(SETTINGS_FILE, JSON.stringify(cfg, null, 4));
    console.log('\n[OK] Settings saved to ' + SETTINGS_FILE);
    console.log('     You can reconfigure anytime with: synapse setup\n');
  } catch (err) {
    rl.close();
    throw err;
  }
}

// ── Process management ────────────────────────────────────────────────────────

function startBackend() {
  const env = {
    ...process.env,
    SYNAPSE_DATA_DIR: DATA_DIR,
    SYNAPSE_BACKEND_PORT: String(BACKEND_PORT),
    PYTHONPATH: BACKEND_DIR + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
  };
  return spawn(venvPython(), [path.join(BACKEND_DIR, 'main.py')], {
    cwd: BACKEND_DIR,
    env,
    stdio: 'inherit',
  });
}

function startFrontend() {
  if (!fs.existsSync(FRONTEND_BUILD)) {
    console.error('Error: bundled frontend not found at', FRONTEND_BUILD);
    console.error('The package may be corrupted. Try reinstalling: npm install -g synapse-orch-ai');
    process.exit(1);
  }
  const env = {
    ...process.env,
    PORT: String(FRONTEND_PORT),
    HOSTNAME: '0.0.0.0',
    BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}`,
    NEXT_PUBLIC_BACKEND_PORT: String(BACKEND_PORT),
    NODE_ENV: 'production',
  };
  return spawn('node', [path.join(FRONTEND_BUILD, 'server.js')], {
    cwd: FRONTEND_BUILD,
    env,
    stdio: 'inherit',
  });
}

function waitForPort(port, name) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const max = 45;
    const check = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/' }, () => {
        console.log(`  ${name} ready.`);
        resolve();
      });
      req.setTimeout(2000);
      req.on('error', () => {
        if (++attempts < max) setTimeout(check, 2000);
        else reject(new Error(`Timeout waiting for ${name} on port ${port}`));
      });
      req.end();
    };
    check();
  });
}

function openBrowser(url) {
  const platform = os.platform();
  const cmd = platform === 'darwin' ? 'open' : platform === 'win32' ? 'cmd' : 'xdg-open';
  const args = platform === 'win32' ? ['/c', 'start', url] : [url];
  setTimeout(() => {
    try { spawn(cmd, args, { detached: true, stdio: 'ignore' }).unref(); } catch (_) {}
  }, 1000);
}

// ── Local install detection ───────────────────────────────────────────────────

const isLocalInstall = PKG_DIR.split(path.sep).includes('node_modules');

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  if (isLocalInstall) {
    console.log('\nTip: Install globally so "synapse" works from anywhere:');
    console.log('  npm install -g synapse-orch-ai\n');
  }

  console.log('Starting Synapse...');
  checkPrerequisites();

  fs.mkdirSync(SYNAPSE_HOME, { recursive: true });

  const ollamaAvail = checkCmd('ollama');

  // Run setup wizard on first launch (no settings.json yet)
  if (!fs.existsSync(SETTINGS_FILE)) {
    if (!ollamaAvail) {
      console.log('\nOllama is not installed -- local models are unavailable.');
    }
    await runSetupWizard(ollamaAvail);
  } else if (!ollamaAvail) {
    console.log("Warning: ollama not found. Local models won't work; cloud API models still work.");
  }

  // Load saved port overrides from settings
  try {
    const saved = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf8'));
    if (saved.backend_port) BACKEND_PORT = saved.backend_port;
    if (saved.frontend_port) FRONTEND_PORT = saved.frontend_port;
  } catch (_) {}

  await setupVenv();
  await installPlaywrightBrowsers();
  ensureDataDir();

  console.log('Starting backend...');
  const backend = startBackend();

  try {
    await waitForPort(BACKEND_PORT, 'Backend');
  } catch (err) {
    console.error(err.message);
    backend.kill();
    process.exit(1);
  }

  console.log('Starting frontend...');
  const frontend = startFrontend();

  try {
    await waitForPort(FRONTEND_PORT, 'Frontend');
  } catch (err) {
    console.error(err.message);
    backend.kill();
    frontend.kill();
    process.exit(1);
  }

  const url = `http://localhost:${FRONTEND_PORT}`;
  openBrowser(url);
  console.log(`\nSynapse is running at ${url}`);
  console.log('Press Ctrl+C to stop.\n');

  function shutdown() {
    console.log('\nStopping Synapse...');
    backend.kill();
    frontend.kill();
    process.exit(0);
  }

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  backend.on('exit', (code) => {
    if (code !== null && code !== 0) {
      console.error(`Backend exited with code ${code}`);
      frontend.kill();
      process.exit(code);
    }
  });
}

// ── Upgrade ───────────────────────────────────────────────────────────────────

async function runUpgrade() {
  console.log('\n=== Synapse AI -- Upgrade ===');
  console.log('\nPulling latest version from npm...');

  const npm = IS_WIN ? (require('child_process').spawnSync('where', ['npm.cmd'], { stdio: 'pipe', shell: true }).stdout?.toString().trim().split('\n')[0]?.trim() || 'npm') : 'npm';

  const result = spawnSync(npm, ['install', '-g', 'synapse-orch-ai@latest'], {
    stdio: 'inherit',
    shell: IS_WIN,
  });

  if (result.status !== 0) {
    console.error('\n  Upgrade failed. Try manually: npm install -g synapse-orch-ai@latest');
    process.exit(1);
  }

  console.log('\n=== Upgrade complete! ===');
  console.log('Run: synapse  (to start the updated Synapse)');
}

// ── Uninstall ─────────────────────────────────────────────────────────────────

async function runUninstall() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const answer = await new Promise((resolve) =>
    rl.question(
      'This will remove ~/.synapse (venv, data, settings).\nType "yes" to confirm: ',
      (a) => { rl.close(); resolve(a.trim().toLowerCase()); }
    )
  );
  if (answer !== 'yes') { console.log('Aborted.'); return; }

  if (fs.existsSync(SYNAPSE_HOME)) {
    console.log(`Removing ${SYNAPSE_HOME} ...`);
    fs.rmSync(SYNAPSE_HOME, { recursive: true, force: true });
    console.log('  Done.');
  } else {
    console.log(`${SYNAPSE_HOME} not found — nothing to remove.`);
  }

  console.log('\nNow run: npm uninstall -g synapse-orch-ai');
}

// ── Entry point ───────────────────────────────────────────────────────────────

const cliArg = process.argv[2];

if (cliArg === 'setup') {
  checkPrerequisites();
  fs.mkdirSync(SYNAPSE_HOME, { recursive: true });
  const ollamaAvail = checkCmd('ollama');
  runSetupWizard(ollamaAvail)
    .then(() => process.exit(0))
    .catch((err) => { console.error('Setup failed:', err.message); process.exit(1); });
} else if (cliArg === 'uninstall') {
  runUninstall()
    .then(() => process.exit(0))
    .catch((err) => { console.error('Uninstall failed:', err.message); process.exit(1); });
} else if (cliArg === 'upgrade') {
  runUpgrade()
    .then(() => process.exit(0))
    .catch((err) => { console.error('Upgrade failed:', err.message); process.exit(1); });
} else {
  main().catch((err) => { console.error('Fatal error:', err.message); process.exit(1); });
}
