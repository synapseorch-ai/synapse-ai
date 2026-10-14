/**
 * What an agent's tools add to every model call, for the agent editors (F483).
 *
 * Every tool an agent holds is declared to the model on every call, so a tool is
 * paid for on each one whether it is used or not. Tools from connected servers
 * are where that grows: acme's Vercel server went from 37 tools to 236 without
 * anyone choosing it, and an agent set to every tool sent 169K tokens a call.
 *
 * The engine bounds what "every tool" takes from connected servers
 * (`ALL_TOOLS_TOKEN_BUDGET`, `core/tools.py`): past it, whole servers are left
 * out, largest first, never a tool the agent named. This file says the same thing
 * before the agent is saved, from each tool's `tokens` as the product's tool list
 * reports it (the engine's `declaration_tokens`: characters / 4). It counts what
 * grows — tools from connected servers and the workspace's own tools — not the
 * product's own, which are a small fixed base. Synced to both products.
 */

/** The engine's `ALL_TOOLS_TOKEN_BUDGET`. */
export const ALL_TOOLS_TOKEN_BUDGET = 40_000;

/** One tool as the editor counts it. */
export type SizedTool = {
  name: string;
  /** About how many tokens its declaration costs a call. */
  tokens: number;
  /** The connected server it comes from; absent for the workspace's own tools. */
  server?: string;
};

export type ToolContext = {
  /** What the agent's tools would add to every call, after anything left out. */
  tokens: number;
  /** Connected servers "every tool" leaves out, largest first. */
  leftOut: { server: string; tokens: number; tools: number }[];
  /** Past the budget: worth a warning. */
  warn: boolean;
  text: string;
};

export function formatTokens(tokens: number): string {
  return tokens >= 1000 ? `${Math.round(tokens / 1000)}K` : String(Math.max(0, Math.round(tokens)));
}

/**
 * What `tools` would add to a call for an agent holding `selected` (its tool
 * names), or every tool when `every` is set — the engine's rule, ahead of a run.
 */
export function toolContext(tools: SizedTool[], selected: ReadonlySet<string>, every: boolean): ToolContext {
  const counted = tools.filter((t) => every || selected.has(t.name));
  // What "every tool" reaches only through itself, by server: the engine's `through_all`.
  const reached = new Map<string, SizedTool[]>();
  if (every) {
    for (const t of counted) {
      if (t.server && !selected.has(t.name)) reached.set(t.server, [...(reached.get(t.server) ?? []), t]);
    }
  }
  const size = (list: SizedTool[]) => list.reduce((sum, t) => sum + (t.tokens || 0), 0);
  let reachedTotal = [...reached.values()].reduce((sum, list) => sum + size(list), 0);
  const leftOut: ToolContext["leftOut"] = [];
  const bySize = [...reached.entries()].sort((a, b) => size(b[1]) - size(a[1]) || a[0].localeCompare(b[0]));
  for (const [server, list] of bySize) {
    if (reachedTotal <= ALL_TOOLS_TOKEN_BUDGET) break;
    leftOut.push({ server, tokens: size(list), tools: list.length });
    reachedTotal -= size(list);
  }
  const dropped = new Set(leftOut.map((x) => x.server));
  const tokens = size(counted.filter((t) => !(t.server && dropped.has(t.server) && !selected.has(t.name))));

  if (leftOut.length) {
    const names = leftOut.map((x) => `${x.server} (${x.tools} tools, about ${formatTokens(x.tokens)})`);
    const which = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
    return {
      tokens, leftOut, warn: true,
      text: `${which} would add more to every model call than "every tool" takes from connected servers ` +
        `(${formatTokens(ALL_TOOLS_TOKEN_BUDGET)}), so runs leave ${leftOut.length === 1 ? "it" : "them"} out. ` +
        `Choose the tools this agent needs to use them. The rest add about ${formatTokens(tokens)} tokens a call.`,
    };
  }
  // Nothing from a connected server or the workspace's own: nothing to say.
  if (tokens <= 0) return { tokens, leftOut, warn: false, text: "" };
  const line = `Tools from connected servers and your own tools add about ${formatTokens(tokens)} tokens to every ` +
    "model call";
  return tokens > ALL_TOOLS_TOKEN_BUDGET
    ? { tokens, leftOut, warn: true, text: `${line}, used or not. Choose only the ones this agent needs.` }
    : { tokens, leftOut, warn: false, text: `${line}.` };
}
