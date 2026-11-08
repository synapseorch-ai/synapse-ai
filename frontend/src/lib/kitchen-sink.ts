/**
 * Whether the design-system review page is reachable.
 *
 * Read in two places that cannot share a check — the middleware, which decides
 * whether the request survives host-based rewriting, and the route itself,
 * which decides whether the page exists. Keeping the rule in one function is
 * what stops those two drifting into a page that routes but 404s, or worse,
 * one that is unreachable locally and live in production.
 *
 * `NODE_ENV` alone is not enough: `docker-compose.dev.yml` runs the web
 * container with `NODE_ENV=production` because it serves a real production
 * build, so a `NODE_ENV` check would hide this page in the environment the
 * team actually develops against. The explicit flag is set there and nowhere
 * in `infra/`.
 *
 * Server-side only — deliberately not `NEXT_PUBLIC_`, so the flag cannot be
 * flipped by anything the browser sends.
 */
export function kitchenSinkEnabled(): boolean {
  return process.env.NODE_ENV !== "production" || process.env.ENABLE_KITCHEN_SINK === "1";
}
