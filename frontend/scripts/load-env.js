#!/usr/bin/env node
/**
 * load-env.js — loads the project-root .env then spawns a Next.js sub-command.
 *
 * Usage (via package.json scripts):
 *   node scripts/load-env.js dev
 *   node scripts/load-env.js start
 *
 * Port resolution priority (mirrors next.config.ts logic):
 *   Frontend : SYNAPSE_FRONTEND_PORT → 3000
 *   Backend  : BACKEND_URL → (derived from SYNAPSE_BACKEND_PORT) → 8765
 *
 * override=false: real shell-exported env vars always win over .env values.
 */

const fs   = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

// ── 1. Load root .env ────────────────────────────────────────────────────────
const rootEnv = path.resolve(__dirname, "../../.env");
if (fs.existsSync(rootEnv)) {
  for (const line of fs.readFileSync(rootEnv, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    if (key && !(key in process.env)) {
      process.env[key] = val;
    }
  }
}

// ── 2. Auto-derive BACKEND_URL from SYNAPSE_BACKEND_PORT if not set ──────────
if (!process.env.BACKEND_URL) {
  const backendPort = process.env.SYNAPSE_BACKEND_PORT || "8765";
  process.env.BACKEND_URL = `http://127.0.0.1:${backendPort}`;
}

// ── 2b. Next.js's own telemetry off (F1567): `next dev` sends it unless told not to.
if (!process.env.NEXT_TELEMETRY_DISABLED) {
  process.env.NEXT_TELEMETRY_DISABLED = "1";
}

// ── 3. Resolve frontend port ─────────────────────────────────────────────────
const port = process.env.SYNAPSE_FRONTEND_PORT || "3000";

// ── 4. Spawn next <cmd> -p <port> ────────────────────────────────────────────
const cmd  = process.argv[2] || "dev";   // "dev" | "start"
const extra = process.argv.slice(3);     // any extra flags forwarded as-is

// On Windows, `next` is installed as next.cmd; Node cannot exec .cmd files
// directly without the shell. Using shell:true works on all platforms.
const isWindows = process.platform === "win32";

const result = spawnSync(
  isWindows ? "next.cmd" : "next",
  [cmd, "-p", port, "-H", "0.0.0.0", ...extra],
  { stdio: "inherit", shell: isWindows }
);

process.exit(result.status != null ? result.status : 0);

