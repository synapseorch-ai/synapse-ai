/* eslint-disable @typescript-eslint/no-explicit-any */

export interface OrchStepType {
  id: string;
  name: string;
  type: string;
  agent_id?: string | null;
  model?: string | null;
  [key: string]: unknown;
}

export interface OrchestrationType {
  id: string;
  name: string;
  description?: string;
  steps?: OrchStepType[];
}

export interface AgentType {
  id: string;
  name: string;
  description?: string;
  tools?: string[];
  model?: string | null;
  provider?: string | null;
  type?: string;
  delegate_agent_ids?: string[];
}

export interface McpServerType {
  name: string;
  label?: string;
  server_type?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  token?: string;
}

export interface CustomToolType {
  name: string;
  generalName?: string;
  description?: string;
  tool_type?: string;
  headers?: Record<string, string>;
}

export interface ExportData {
  orchestrations: OrchestrationType[];
  agents: AgentType[];
  mcp_servers: McpServerType[];
  custom_tools: CustomToolType[];
}

export interface ImportBundle {
  synapse_export: boolean;
  version?: string | number;
  /** "self-host" or "cloud": which product wrote the file (F464). */
  exported_from?: string;
  exported_at?: string;
  has_python_tools?: boolean;
  orchestrations: OrchestrationType[];
  agents: AgentType[];
  mcp_servers: McpServerType[];
  custom_tools: CustomToolType[];
}

export interface ImportResult {
  id?: string;
  name?: string;
  label?: string;
  status:
    | "imported" | "replaced" | "skipped" | "skipped_existing" | "error" | "connected" | "disconnected"
    | "oauth_pending";
  message?: string;
  auth_url?: string;
}

/** Something imported that can't run here yet: a model, a tool, a missing workflow or agent (F464). */
export interface ImportWarning {
  kind: string;
  id: string;
  name: string;
  message: string;
}
