/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useCallback } from "react";
import {
  Download, Package, AlertTriangle, XCircle, Loader2,
  Workflow, Bot, Server, Wrench,
} from "lucide-react";
import type { ExportData } from "./types";
import { orchAgentDeps, agentMcpDeps, agentToolDeps, agentDelegateDeps } from "./utils";
import { SectionTable } from "./SectionTable";

const inputCls = "w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none placeholder:text-zinc-700";

export function ExportView() {
  const [data, setData] = useState<ExportData | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportName, setExportName] = useState(`synapse_export_${new Date().toISOString().slice(0, 10)}`);

  const [selOrch, setSelOrch] = useState<Set<string>>(new Set());
  const [selAgent, setSelAgent] = useState<Set<string>>(new Set());
  const [selMcp, setSelMcp] = useState<Set<string>>(new Set());
  const [selTool, setSelTool] = useState<Set<string>>(new Set());
  const [lockedAgent, setLockedAgent] = useState<Set<string>>(new Set());
  const [lockedMcp, setLockedMcp] = useState<Set<string>>(new Set());
  const [lockedTool, setLockedTool] = useState<Set<string>>(new Set());

  const loadData = async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch("/api/export/data");
      if (!res.ok) throw new Error("Failed to load export data");
      setData(await res.json());
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  const recalc = useCallback((newSelOrch: Set<string>, newSelAgent: Set<string>, d: ExportData) => {
    const orchLockedAgents = new Set<string>();
    for (const oid of newSelOrch) {
      const o = d.orchestrations.find(x => x.id === oid);
      if (o) orchAgentDeps(o).forEach(a => orchLockedAgents.add(a));
    }
    const delegateLockedAgents = new Set<string>();
    const allSelectedAgents = new Set([...newSelAgent, ...orchLockedAgents]);
    for (const aid of allSelectedAgents) {
      const ag = d.agents.find(a => a.id === aid);
      if (ag) agentDelegateDeps(ag).forEach(id => delegateLockedAgents.add(id));
    }
    const allAgents = new Set([...allSelectedAgents, ...delegateLockedAgents]);
    const agentLockedMcp = new Set<string>();
    const agentLockedTool = new Set<string>();
    for (const aid of allAgents) {
      const ag = d.agents.find(a => a.id === aid);
      if (ag) {
        agentMcpDeps(ag, d.mcp_servers).forEach(m => agentLockedMcp.add(m));
        agentToolDeps(ag, d.custom_tools).forEach(t => agentLockedTool.add(t));
      }
    }
    const allLockedAgents = new Set([...orchLockedAgents, ...delegateLockedAgents]);
    setLockedAgent(allLockedAgents);
    setLockedMcp(agentLockedMcp);
    setLockedTool(agentLockedTool);
    setSelAgent(prev => new Set([...prev, ...allLockedAgents]));
    setSelMcp(prev => new Set([...prev, ...agentLockedMcp]));
    setSelTool(prev => new Set([...prev, ...agentLockedTool]));
  }, []);

  const toggleOrch = (id: string, checked: boolean) => {
    const next = new Set(selOrch); checked ? next.add(id) : next.delete(id);
    setSelOrch(next); if (data) recalc(next, selAgent, data);
  };
  const toggleAgent = (id: string, checked: boolean) => {
    if (lockedAgent.has(id)) return;
    const next = new Set(selAgent); checked ? next.add(id) : next.delete(id);
    setSelAgent(next); if (data) recalc(selOrch, next, data);
  };
  const toggleMcp = (name: string, checked: boolean) => {
    if (lockedMcp.has(name)) return;
    const next = new Set(selMcp); checked ? next.add(name) : next.delete(name); setSelMcp(next);
  };
  const toggleTool = (name: string, checked: boolean) => {
    if (lockedTool.has(name)) return;
    const next = new Set(selTool); checked ? next.add(name) : next.delete(name); setSelTool(next);
  };

  const hasPythonTool = data?.custom_tools.some(t => selTool.has(t.name) && t.tool_type === "python");
  const total = selOrch.size + selAgent.size + selMcp.size + selTool.size;

  const handleExport = async () => {
    if (total === 0) return;
    setExporting(true);
    try {
      const res = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orchestration_ids: [...selOrch], agent_ids: [...selAgent], mcp_server_names: [...selMcp], custom_tool_names: [...selTool] }),
      });
      if (!res.ok) throw new Error("Export failed");
      const blob = new Blob([JSON.stringify(await res.json(), null, 2)], { type: "application/json" });
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `${exportName || "synapse_export"}.json` });
      a.click(); URL.revokeObjectURL(a.href);
    } catch (e: any) { setError(e.message); }
    finally { setExporting(false); }
  };

  if (!data) {
    return (
      <div className="space-y-4">
        <div className="p-8 text-center border border-dashed border-zinc-800 bg-zinc-900/30">
          <Package className="h-8 w-8 mx-auto text-zinc-700 mb-3" />
          <p className="text-zinc-400 text-sm font-bold">Export your configurations</p>
          <p className="text-zinc-600 text-xs mt-1">Select orchestrations, agents, MCP servers, and tools to export as a portable bundle</p>
          <button
            onClick={loadData}
            disabled={loading}
            className="mt-4 flex items-center gap-2 px-5 py-2 bg-white text-black text-sm font-bold hover:bg-zinc-200 transition-colors disabled:opacity-50 mx-auto"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Package className="h-4 w-4" />}
            {loading ? "Loading…" : "Load Export Data"}
          </button>
        </div>
        {error && (
          <div className="flex items-center gap-2 text-red-400 text-xs p-3 border border-red-900/50 bg-red-950/10">
            <XCircle className="h-4 w-4 flex-shrink-0" /> {error}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">

      {hasPythonTool && (
        <div className="flex items-start gap-3 p-4 border border-yellow-900/50 bg-yellow-950/10">
          <AlertTriangle className="h-4 w-4 text-yellow-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-yellow-400 text-xs font-bold uppercase tracking-wider">Python Tool Warning</p>
            <p className="text-yellow-600 text-xs mt-1 leading-relaxed">
              One or more selected agents use a custom Python tool. Python code is exported as-is —
              make sure it does <strong className="text-yellow-500">not contain hardcoded secrets, API keys, or passwords</strong>.
            </p>
          </div>
        </div>
      )}

      <div className="space-y-0 border border-zinc-800 divide-y divide-zinc-800">
        <SectionTable title="Orchestrations" icon={Workflow} items={data.orchestrations} selected={selOrch} locked={new Set()} getId={o => o.id} getLabel={o => o.name} getDesc={o => o.description} onToggle={toggleOrch} />
        <SectionTable title="Agents" icon={Bot} items={data.agents} selected={selAgent} locked={lockedAgent} getId={a => a.id} getLabel={a => a.name} getDesc={a => a.description} onToggle={toggleAgent} />
        <SectionTable title="MCP Servers" icon={Server} items={data.mcp_servers} selected={selMcp} locked={lockedMcp} getId={m => m.name} getLabel={m => m.label || m.name} onToggle={toggleMcp} />
        <SectionTable title="Custom Tools" icon={Wrench} items={data.custom_tools} selected={selTool} locked={lockedTool} getId={t => t.name} getLabel={t => t.generalName || t.name} getDesc={t => t.description} onToggle={toggleTool} />
      </div>

      {(data.orchestrations.length + data.agents.length + data.mcp_servers.length + data.custom_tools.length) === 0 && (
        <p className="text-zinc-600 text-sm text-center py-4">Nothing to export yet — add orchestrations, agents, MCP servers, or tools first.</p>
      )}

      {total > 0 && (
        <div className="border border-zinc-800 bg-zinc-900/40 p-5 space-y-4">
          <h4 className="text-[10px] uppercase font-bold tracking-wider text-zinc-500">Export File</h4>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={exportName}
              onChange={e => setExportName(e.target.value)}
              className={`${inputCls} flex-1`}
              placeholder="synapse_export"
            />
            <span className="text-zinc-600 text-sm font-mono">.json</span>
          </div>
          <div className="flex items-center justify-between gap-4 pt-1">
            <p className="text-xs text-zinc-500">
              <span className="text-white font-bold">{total}</span> item{total !== 1 ? "s" : ""} selected
              {selOrch.size > 0 && ` · ${selOrch.size} orchestration${selOrch.size !== 1 ? "s" : ""}`}
              {selAgent.size > 0 && ` · ${selAgent.size} agent${selAgent.size !== 1 ? "s" : ""}`}
              {selMcp.size > 0 && ` · ${selMcp.size} MCP server${selMcp.size !== 1 ? "s" : ""}`}
              {selTool.size > 0 && ` · ${selTool.size} tool${selTool.size !== 1 ? "s" : ""}`}
            </p>
            <button
              onClick={handleExport}
              disabled={exporting}
              className="flex items-center gap-2 px-6 py-2 bg-white text-black text-sm font-bold hover:bg-zinc-200 transition-colors disabled:opacity-50 shrink-0"
            >
              {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
              {exporting ? "Exporting…" : "Export & Download"}
            </button>
          </div>
          {error && <p className="text-red-400 text-xs">{error}</p>}
        </div>
      )}
    </div>
  );
}
