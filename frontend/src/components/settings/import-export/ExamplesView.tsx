/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";
/*
 * The example packs: Workflows → Templates, and Settings → Import / Export →
 * Examples (the same list in both places, D214).
 *
 * Each pack is a card with the path of each workflow it holds, step by step
 * and coloured by who does the work (D211), and "You approve before it
 * finishes" in amber when a person takes part (M2f). The paths come from the
 * packs' bundles, fetched once after the list; "Preview & Import" reuses them.
 */

import { useEffect, useState } from "react";
import { AlertCircle, Bot, ChevronRight, PackageOpen, Plug, User, Workflow, Wrench } from "lucide-react";

import { Badge, Button, Spinner } from "@/components/ui";
import { StepPath, templatePath } from "@/components/run";

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
  /** The "Example Packs" heading: Import / Export shows it; Templates' frame already titles the page. */
  showHeading?: boolean;
}

// What people see (D209): Orchestrations are Workflows, MCP servers are Apps.
const TAG_LABELS: Record<string, string> = {
  agents: "Agents",
  orchestrations: "Workflows",
  mcp_servers: "Apps",
  custom_tools: "Tools",
};

function Count({ icon: Icon, count, label }: { icon: React.FC<{ className?: string }>; count: number; label: string }) {
  if (count === 0) return null;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Icon className="size-3.5 text-text-faint" aria-hidden />
      {count} {label}{count !== 1 ? "s" : ""}
    </span>
  );
}

function WorkflowPath({ workflow }: { workflow: any }) {
  const { types, hasPerson } = templatePath(workflow);
  return (
    <li className="grid gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-xs">
        <span className="min-w-0 truncate font-medium text-text">{workflow.name}</span>
        {hasPerson ? (
          <span className="inline-flex items-center gap-1 font-medium text-person-ink">
            <User className="size-3.5" aria-hidden />
            You approve before it finishes
          </span>
        ) : (
          <span className="text-text-faint">Runs on its own</span>
        )}
      </div>
      <StepPath types={types} />
    </li>
  );
}

export function ExamplesView({ onLoadBundle, showHeading = true }: ExamplesViewProps) {
  const [packs, setPacks] = useState<ExamplePack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  // Each pack's bundle, once fetched: its workflows draw the card's paths.
  const [bundles, setBundles] = useState<Record<string, any>>({});

  useEffect(() => {
    let live = true;
    fetch("/api/examples")
      .then(r => {
        if (!r.ok) throw new Error("Failed to fetch examples");
        return r.json();
      })
      .then((data: ExamplePack[]) => {
        if (!live) return;
        setPacks(data);
        setLoading(false);
        // The paths: one bundle per pack that holds a workflow. A failure only
        // leaves that card without its paths.
        for (const pack of data) {
          if (!pack.orchestration_count) continue;
          fetch(`/api/examples/${pack.id}`)
            .then(r => (r.ok ? r.json() : null))
            .then(bundle => { if (live && bundle) setBundles(b => ({ ...b, [pack.id]: bundle })); })
            .catch(() => {});
        }
      })
      .catch(e => { if (live) { setError(e.message); setLoading(false); } });
    return () => { live = false; };
  }, []);

  const handleImport = async (pack: ExamplePack) => {
    if (bundles[pack.id]) { onLoadBundle(bundles[pack.id]); return; }
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
        <Spinner size={24} className="text-machine" />
        <span className="ml-3 text-sm text-text-faint">Loading example packs…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="flex items-start gap-3 rounded-lg border border-danger/25 bg-danger-subtle p-4 text-danger">
        <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div>
          <p className="text-sm font-semibold">Failed to load examples</p>
          <p className="mt-1 text-sm">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {showHeading && (
        <div>
          <h2 className="text-section text-text">Example Packs</h2>
          <p className="mt-1 text-sm text-text-muted">
            Curated collections of agents, workflows and apps. Select a pack to preview what will be imported before committing.
          </p>
        </div>
      )}

      <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(100%,22rem),1fr))]">
        {packs.map(pack => {
          const isLoading = loadingId === pack.id;
          const workflows: any[] = bundles[pack.id]?.orchestrations ?? [];
          return (
            <article
              key={pack.id}
              data-pack={pack.id}
              className="group flex flex-col gap-3 rounded-xl border border-border bg-surface p-4 transition-shadow hover:border-border-strong hover:shadow-elevated"
            >
              <div className="flex flex-wrap gap-1.5">
                {pack.tags.map(tag => (
                  <Badge key={tag} size="sm">{TAG_LABELS[tag] || tag}</Badge>
                ))}
              </div>

              <div>
                <h3 className="text-[15px] font-semibold tracking-[-0.01em] text-text">{pack.name}</h3>
                <p className="mt-1 text-sm leading-relaxed text-text-muted">{pack.description}</p>
              </div>

              {pack.orchestration_count > 0 && (
                workflows.length ? (
                  <ul className="grid gap-3" aria-label={`Workflows in ${pack.name}`}>
                    {workflows.map((w, i) => <WorkflowPath key={w.id ?? i} workflow={w} />)}
                  </ul>
                ) : (
                  <div aria-hidden className="h-[46px] animate-pulse rounded-lg bg-surface-2" />
                )
              )}

              <div className="mt-auto flex flex-wrap items-center justify-between gap-3 pt-1">
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
                  <Count icon={Bot} count={pack.agent_count} label="Agent" />
                  <Count icon={Workflow} count={pack.orchestration_count} label="Workflow" />
                  <Count icon={Plug} count={pack.mcp_count} label="App" />
                  <Count icon={Wrench} count={pack.tool_count} label="Tool" />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => handleImport(pack)}
                  disabled={!!loadingId}
                  loading={isLoading}
                >
                  {isLoading ? "Loading…" : <>Preview &amp; Import<ChevronRight className="size-3.5" aria-hidden /></>}
                </Button>
              </div>
            </article>
          );
        })}
      </div>

      {packs.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16">
          <PackageOpen className="mb-3 size-10 text-text-faint" aria-hidden />
          <p className="text-sm text-text-faint">No example packs available.</p>
        </div>
      )}
    </div>
  );
}
