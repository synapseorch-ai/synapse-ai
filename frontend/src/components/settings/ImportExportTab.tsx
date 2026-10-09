/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useCallback, useEffect } from "react";
import { Download, Upload, Sparkles } from "lucide-react";
import { ExportView } from "./import-export/ExportView";
import { ImportView } from "./import-export/ImportView";
import { ExamplesView } from "./import-export/ExamplesView";
import { cn, focusRing } from "@/lib/cn";

type TabView = "export" | "import" | "examples";

/** A view pill, as Usage's provider pills: the chosen one on a quiet ground, the rest plain text. */
const pill = (active: boolean) =>
  cn(
    "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-colors",
    focusRing,
    active
      ? "border-border-strong bg-surface-2 text-text"
      : "border-transparent text-text-muted hover:bg-surface-2 hover:text-text",
  );

export function ImportExportTab({ defaultView, onImportSuccess, onNavigate }: { defaultView?: TabView; onImportSuccess?: () => void; onNavigate?: (tab: string) => void }) {
  const [view, setView] = useState<TabView>(defaultView ?? "export");
  // When a bundle is loaded from ExamplesView, store it so ImportView can consume it
  const [preloadedBundle, setPreloadedBundle] = useState<any>(null);

  useEffect(() => {
    if (defaultView) {
      setView(defaultView);
    }
  }, [defaultView]);

  const handleLoadBundle = useCallback((bundle: any) => {
    setPreloadedBundle(bundle);
    setView("import");
  }, []);

  const handleImportReset = useCallback(() => {
    setPreloadedBundle(null);
  }, []);

  return (
    <div className="space-y-8">
      {/* View toggle */}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={() => setView("export")}
          aria-pressed={view === "export"}
          className={pill(view === "export")}
        >
          <Download className="h-3.5 w-3.5" /> Export
        </button>
        <button
          type="button"
          onClick={() => { setView("import"); setPreloadedBundle(null); }}
          aria-pressed={view === "import"}
          className={pill(view === "import")}
        >
          <Upload className="h-3.5 w-3.5" /> Import
        </button>
        <button
          type="button"
          onClick={() => setView("examples")}
          aria-pressed={view === "examples"}
          className={pill(view === "examples")}
        >
          <Sparkles className="h-3.5 w-3.5" /> Examples
        </button>
      </div>

      {view === "export" && <ExportView />}
      {view === "examples" && <ExamplesView onLoadBundle={handleLoadBundle} />}
      {view === "import" && (
        <ImportView
          preloadedBundle={preloadedBundle}
          onReset={handleImportReset}
          onImportSuccess={onImportSuccess}
          onNavigate={onNavigate}
        />
      )}
    </div>
  );
}
