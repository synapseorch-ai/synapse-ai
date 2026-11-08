import type { OrchestrationType, AgentType, McpServerType, CustomToolType, ImportBundle } from "./types";

/**
 * A file as the importer reads it, or null when it isn't one (F464). Both products write the
 * envelope (`synapse_export`, "1.0" or "1.1"); Cloud's file from before it says `version: 1`
 * and keeps its tools under `tools`. A workflow with no id is keyed by its place in the file,
 * `#<index>`, which is the key the server reads, and it gets a real id when it is imported.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeBundle(parsed: any): ImportBundle | null {
  if (!parsed || typeof parsed !== "object") return null;
  const lists = ["orchestrations", "agents", "tools", "mcp_servers"];
  const oldCloud = parsed.version === 1 && lists.some(k => Array.isArray(parsed[k]));
  if (!parsed.synapse_export && !oldCloud) return null;
  const list = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
  return {
    ...parsed,
    synapse_export: true,
    exported_from: parsed.exported_from ?? (oldCloud ? "cloud" : undefined),
    orchestrations: list<OrchestrationType>(parsed.orchestrations).map((o, i) => ({ ...o, id: o?.id || `#${i}` })),
    agents: list<AgentType>(parsed.agents),
    mcp_servers: list<McpServerType>(parsed.mcp_servers),
    custom_tools: Array.isArray(parsed.custom_tools) ? parsed.custom_tools : list<CustomToolType>(parsed.tools),
  };
}

export function orchAgentDeps(orch: OrchestrationType): string[] {
  return (orch.steps || []).map(s => s.agent_id).filter(Boolean) as string[];
}

export function agentMcpDeps(agent: AgentType, allMcp: McpServerType[]): string[] {
  const tools = agent.tools || [];
  return allMcp.filter(m => tools.some(t => t.startsWith(`${m.name}__`))).map(m => m.name);
}

export function agentToolDeps(agent: AgentType, allTools: CustomToolType[]): string[] {
  return allTools.filter(t => (agent.tools || []).includes(t.name)).map(t => t.name);
}

export function agentDelegateDeps(agent: AgentType): string[] {
  // Non-empty = explicit sub-agent list; empty = "any agent" (no specific deps to lock)
  return agent.delegate_agent_ids || [];
}
