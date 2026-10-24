/*
 * Addresses whose screens moved, and where each went.
 *
 * Deliberately dependency-free: next.config.ts imports this to turn them into
 * real 308s at the routing layer, and it must not drag React or lucide-react
 * into the config load. nav.ts re-exports it so there is still one list.
 *
 * Why the routing layer and not `redirect()` in the page: the app shell is a
 * client layout, so its HTML starts streaming before the page component runs.
 * A redirect thrown at that point arrives in the RSC flight payload instead of
 * as an HTTP status — the browser does follow it, but the response is a 200
 * and the user sees the shell flash first.
 *
 * Every target is the screen's final address, never another moved one: a chain
 * of 308s costs a round trip per hop, and a browser caches each permanently.
 */

/** Old /settings/<tab> URLs whose screens moved out to the rail. */
export const MOVED_TABS: Record<string, string> = {
    orchestrations: '/workflows',
    agents: '/agents',
    custom_tools: '/tools',
    mcp_servers: '/apps',
    schedules: '/schedules',
    vault: '/vault',
    logs: '/logs',
    usage: '/usage',
    // Out of Settings into the Channels hub (D209, M1a).
    messaging: '/channels',
    widgets: '/widgets',
};

/**
 * Rail pages renamed in what people see (D209, M1a): Orchestrations became
 * Workflows and MCP servers became Apps. The engine's and the API's names did
 * not change, so `/api/orchestrations` is untouched; only the page moved.
 */
export const MOVED_ROUTES: Record<string, string> = {
    '/orchestrations': '/workflows',
    '/mcp-servers': '/apps',
    // Connect › AI tools left the rail for Settings › MCP server and Home's card (GUIDE1, F1027).
    '/ai-tools': '/settings/mcp_server',
};
