/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useEffect } from "react";
import { Spinner } from '@/components/ui';
import {
  PackageOpen, Bot, Workflow, Server, Wrench,
  Download, AlertCircle, ChevronRight, Sparkles,
} from "lucide-react";

interface ExamplePack {
  id: string;
  name: string;
  description: string;
  tags: string[];
  agent_count: number;
  orchestration_count: number;
  mcp_count: number;
  tool_count: number;
  file: string;
}

interface ExamplesViewProps {
  /** Called when the user clicks "Preview & Import" on a pack — parent switches to ImportView with this bundle */
  onLoadBundle: (bundle: any) => void;
}

const TAG_STYLES: Record<string, string> = {
  agents: "text-accent bg-accent-subtle border-accent/40",
  orchestrations: "text-emerald-400 bg-emerald-950/60 border-emerald-800/50",
  mcp_servers: "text-amber-400 bg-amber-950/60 border-amber-800/50",
  custom_tools: "text-sky-400 bg-sky-950/60 border-sky-800/50",
};

const TAG_LABELS: Record<string, string> = {
  agents: "Agents",
  orchestrations: "Orchestrations",
  mcp_servers: "MCP Servers",
  custom_tools: "Custom Tools",
};

function CountPill({ icon: Icon, count, label, color }: { icon: any; count: number; label: string; color: string }) {
  if (count === 0) return null;
  return (
    <div className={`flex items-center gap-1 text-[11px] font-mono ${color}`}>
      <Icon className="h-3 w-3" />
      <span>{count} {label}{count !== 1 ? "s" : ""}</span>
    </div>
  );
}

export function ExamplesView({ onLoadBundle }: ExamplesViewProps) {
  const [packs, setPacks] = useState<ExamplePack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/examples")
      .then(r => {
        if (!r.ok) throw new Error("Failed to fetch examples");
        return r.json();
      })
      .then(data => { setPacks(data); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, []);

  const handleImport = async (pack: ExamplePack) => {
    setLoadingId(pack.id);
    try {
      const res = await fetch(`/api/examples/${pack.id}`);
      if (!res.ok) throw new Error("Failed to load example pack");
      const bundle = await res.json();
      onLoadBundle(bundle);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoadingId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner size={24} className="text-text-faint" />
        <span className="ml-3 text-sm text-text-faint">Loading example packs…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-3 p-4 border border-red-900/50 bg-red-950/10 rounded-md">
        <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0 mt-0.5" />
        <div>
          <p className="text-red-400 text-xs font-bold uppercase tracking-wider">Failed to load examples</p>
          <p className="text-red-600 text-xs mt-1">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start gap-3 px-4 py-3 border border-zinc-800 bg-zinc-900/60 rounded-md">
        <Sparkles className="h-4 w-4 text-accent flex-shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-bold text-zinc-200">Example Packs</p>
          <p className="text-[11px] text-text-faint mt-0.5 leading-relaxed">
            Curated collections of agents, orchestrations, and MCP servers. Select a pack to preview what will be imported before committing.
          </p>
        </div>
      </div>

      {/* Pack Grid */}
      <div className="grid gap-3">
        {packs.map(pack => {
          const isLoading = loadingId === pack.id;
          return (
            <div
              key={pack.id}
              className="group flex flex-col border border-zinc-800 bg-zinc-900/30 hover:bg-zinc-900/70 hover:border-zinc-700 transition-all duration-200 rounded-md"
            >
              <div className="flex items-start justify-between gap-4 p-4">
                {/* Left */}
                <div className="flex-1 min-w-0">
                  {/* Tags */}
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {pack.tags.map(tag => (
                      <span
                        key={tag}
                        className={`text-[9px] font-mono uppercase tracking-widest px-1.5 py-0.5 border ${TAG_STYLES[tag] || "text-zinc-400 bg-zinc-900 border-zinc-700"} rounded-md`}
                      >
                        {TAG_LABELS[tag] || tag}
                      </span>
                    ))}
                  </div>

                  {/* Name */}
                  <p className="text-sm font-bold text-zinc-100 mb-1">{pack.name}</p>

                  {/* Description */}
                  <p className="text-[12px] text-zinc-400 leading-relaxed">{pack.description}</p>

                  {/* Counts */}
                  <div className="flex flex-wrap gap-3 mt-2.5">
                    <CountPill icon={Bot} count={pack.agent_count} label="Agent" color="text-accent" />
                    <CountPill icon={Workflow} count={pack.orchestration_count} label="Orchestration" color="text-emerald-400" />
                    <CountPill icon={Server} count={pack.mcp_count} label="MCP Server" color="text-amber-400" />
                    <CountPill icon={Wrench} count={pack.tool_count} label="Tool" color="text-sky-400" />
                  </div>
                </div>

                {/* Action */}
                <button
                  onClick={() => handleImport(pack)}
                  disabled={!!loadingId}
                  className="flex items-center gap-1.5 shrink-0 px-3 py-2 bg-zinc-800 border border-zinc-700 text-zinc-300 hover:bg-white hover:text-black hover:border-white text-xs font-bold transition-all duration-150 disabled:opacity-50 group-hover:border-zinc-500 rounded-md"
                >
                  {isLoading ? (
                    <>
                      <Spinner size={14} />
                      Loading…
                    </>
                  ) : (
                    <>
                      <Download className="h-3.5 w-3.5" />
                      Preview &amp; Import
                      <ChevronRight className="h-3 w-3 ml-0.5" />
                    </>
                  )}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {packs.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16">
          <PackageOpen className="h-10 w-10 text-text-faint mb-3" />
          <p className="text-sm text-text-faint">No example packs available.</p>
        </div>
      )}
    </div>
  );
}
