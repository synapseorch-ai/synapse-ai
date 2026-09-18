/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import { useState, useCallback, useEffect } from "react";
import { Download, Upload, Sparkles } from "lucide-react";
import { ExportView } from "./import-export/ExportView";
import { ImportView } from "./import-export/ImportView";
import { ExamplesView } from "./import-export/ExamplesView";

type TabView = "export" | "import" | "examples";

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
      <div className="flex items-center gap-1 bg-zinc-900 border border-zinc-800 p-1 w-fit rounded-md">
        <button
          onClick={() => setView("export")}
          className={`flex items-center gap-1.5 px-5 py-2 text-xs font-bold transition-colors ${view === "export" ? "bg-accent text-accent-fg" : "text-text-faint hover:text-white"}`}
        >
          <Download className="h-3.5 w-3.5" /> Export
        </button>
        <button
          onClick={() => { setView("import"); setPreloadedBundle(null); }}
          className={`flex items-center gap-1.5 px-5 py-2 text-xs font-bold transition-colors ${view === "import" ? "bg-accent text-accent-fg" : "text-text-faint hover:text-white"}`}
        >
          <Upload className="h-3.5 w-3.5" /> Import
        </button>
        <button
          onClick={() => setView("examples")}
          className={`flex items-center gap-1.5 px-5 py-2 text-xs font-bold transition-colors ${view === "examples" ? "bg-accent text-accent-fg" : "text-text-faint hover:text-white"}`}
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
