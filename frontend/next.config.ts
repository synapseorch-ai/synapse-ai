import type { NextConfig } from "next";
import { URL } from "url";
import path from "path";
import fs from "fs";

// ---------------------------------------------------------------------------
// Load the project-root .env before reading any env vars.
// This lets `npm run dev` / `npm start` pick up BACKEND_URL, ports, etc.
// without requiring the user to export them manually in their shell.
// override=false — real environment variables (already exported in the shell)
// always take precedence over the values in the file.
// ---------------------------------------------------------------------------
const _rootEnv = path.resolve(__dirname, "../.env");
if (fs.existsSync(_rootEnv)) {
  const _lines = fs.readFileSync(_rootEnv, "utf-8").split("\n");
  for (const _line of _lines) {
    const _trimmed = _line.trim();
    if (!_trimmed || _trimmed.startsWith("#")) continue;
    const _eq = _trimmed.indexOf("=");
    if (_eq === -1) continue;
    const _key = _trimmed.slice(0, _eq).trim();
    const _val = _trimmed.slice(_eq + 1).trim().replace(/^["']|["']$/g, "");
    if (_key && !(_key in process.env)) {
      process.env[_key] = _val;
    }
  }
}

// ---------------------------------------------------------------------------
// Resolve BACKEND_URL — single source of truth for all server-side routes.
//
// Priority:
//   1. BACKEND_URL already in env  (explicit — use as-is)
//   2. SYNAPSE_BACKEND_PORT in env (auto-build http://127.0.0.1:<port>)
//   3. Hard default                (http://127.0.0.1:8765)
//
// This means a user only needs to set SYNAPSE_BACKEND_PORT once and
// everything — rewrites, API routes, port display — picks it up correctly.
// ---------------------------------------------------------------------------
const _backendPort = process.env.SYNAPSE_BACKEND_PORT || "8765";
const _derivedUrl  = `http://127.0.0.1:${_backendPort}`;

// If BACKEND_URL not set at all, use the derived value.
if (!process.env.BACKEND_URL) {
  process.env.BACKEND_URL = _derivedUrl;
}

const BACKEND_URL   = process.env.BACKEND_URL!;
const _parsedBackend = new URL(BACKEND_URL);
const BACKEND_PORT  = _parsedBackend.port || _backendPort;

// Also resolve the frontend port for completeness (used by load-env.js, but
// setting it here too keeps the config self-consistent).
const _frontendPort = process.env.SYNAPSE_FRONTEND_PORT || "3000";

const nextConfig: NextConfig = {
  output: "standalone",
  env: {
    // These are injected into every server-side page/route at runtime.
    // Route handlers can read process.env.BACKEND_URL without their own fallback.
    BACKEND_URL,
    BACKEND_PORT,
    // Inject secrets so they are available in the Edge Runtime middleware bundle.
    // (Edge Runtime only sees vars in this block or NEXT_PUBLIC_* — raw process.env
    //  assignments from earlier in this file are NOT guaranteed to reach the Edge.)
    SYNAPSE_JWT_SECRET: process.env.SYNAPSE_JWT_SECRET || '',
    SYNAPSE_INTERNAL_TOKEN: process.env.SYNAPSE_INTERNAL_TOKEN || '',
    // Expose backend port to client-side code (e.g., for UI instructions)
    NEXT_PUBLIC_BACKEND_PORT: BACKEND_PORT,
    NEXT_PUBLIC_FRONTEND_PORT: _frontendPort,
  },
  async rewrites() {
    return {
      beforeFiles: [],
      afterFiles: [],
      // fallback runs only when no app router route.ts matches
      fallback: [
        {
          source: "/auth/:path*",
          destination: `${BACKEND_URL}/auth/:path*`,
        },
        {
          source: "/api/:path*",
          destination: `${BACKEND_URL}/api/:path*`,
        },
      ],
    };
  },
};

export default nextConfig;

