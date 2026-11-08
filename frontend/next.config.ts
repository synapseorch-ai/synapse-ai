import type { NextConfig } from "next";
import path from "path";
import fs from "fs";
import { MOVED_ROUTES, MOVED_TABS } from "./src/lib/moved-routes";
import { MAX_REQUEST_BODY_BYTES } from "./src/lib/requestBodyLimit";

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
// The backend's address is NOT resolved here (F309). Anything this file puts
// in `env` or in a rewrite is fixed at build: a packaged UI then called the port
// it was built for, whatever `synapse start` passed. Server code reads
// `BACKEND_URL` (else `SYNAPSE_BACKEND_PORT`, else 8765) when it starts —
// `src/lib/backend.ts` — and /api and /auth calls no route handler answers are
// proxied there by the catch-alls (`src/lib/backendProxy.ts`).
// ---------------------------------------------------------------------------
const _frontendPort = process.env.SYNAPSE_FRONTEND_PORT || "3000";

const nextConfig: NextConfig = {
  output: "standalone",
  env: {
    // Inlined at BUILD time, so only what is fixed for a build belongs here. The
    // backend's address and the per-install secrets (SYNAPSE_INTERNAL_TOKEN,
    // SYNAPSE_JWT_SECRET) are read from the live process.env instead: proxy.ts
    // and route handlers run on Node, and read them at request time.
    NEXT_PUBLIC_FRONTEND_PORT: _frontendPort,
  },
  experimental: {
    // How much of a request's body proxy.ts's copy keeps; past it the backend
    // got a cut body under the full Content-Length (F364). See requestBodyLimit.ts.
    proxyClientMaxBodySize: MAX_REQUEST_BODY_BYTES,
  },
  async redirects() {
    // Screens that left /settings/* for the rail, and rail pages that were
    // renamed. These URLs are in browser history and were in the app's own
    // buttons; a bookmark that 404s is a bug report. Permanent, and the query string rides along — which is what keeps
    // /settings/orchestrations?run=<id> landing on the right run.
    return [
      // Bare /settings used to 404 — there was never a page at that level.
      { source: "/settings", destination: "/settings/general", permanent: false },
      ...Object.entries(MOVED_TABS).map(([tab, destination]) => ({
        source: `/settings/${tab}`,
        destination,
        permanent: true,
      })),
      // Pages renamed in what people see (D209). The query string rides along
      // here too, so /orchestrations?run=<id> still opens that run.
      ...Object.entries(MOVED_ROUTES).map(([source, destination]) => ({
        source,
        destination,
        permanent: true,
      })),
    ];
  },
};

export default nextConfig;

