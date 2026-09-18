"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

export function SectionTable<T>({
  title, icon: Icon, items, selected, locked,
  getId, getLabel, getDesc, onToggle,
}: {
  title: string;
  icon: React.ElementType;
  items: T[];
  selected: Set<string>;
  locked: Set<string>;
  getId: (i: T) => string;
  getLabel: (i: T) => string;
  getDesc?: (i: T) => string | undefined;
  onToggle: (id: string, checked: boolean) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  if (items.length === 0) return null;

  const selectedCount = items.filter(i => selected.has(getId(i))).length;

  return (
    <div className="border border-zinc-800">
      <button
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center gap-2 px-4 py-3 bg-zinc-900 hover:bg-zinc-800 transition-colors"
      >
        <Icon className="h-4 w-4 text-zinc-400" />
        <span className="text-xs font-bold uppercase tracking-wider text-zinc-400">{title}</span>
        <span className="ml-auto flex items-center gap-2 text-[10px] text-text-faint font-mono">
          {selectedCount}/{items.length}
          {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </span>
      </button>

      {!collapsed && (
        <div className="divide-y divide-zinc-800/60 max-h-[400px] overflow-y-auto">
          {items.map((item, index) => {
            const id = getId(item) ?? `item-${index}`;
            const isSelected = selected.has(getId(item));
            const isLocked = locked.has(getId(item));
            const desc = getDesc?.(item);
            return (
              <div
                key={id || `fallback-${index}`}
                role="checkbox"
                aria-checked={isSelected}
                onClick={() => {
                  const rawId = getId(item);
                  if (!isLocked && rawId) onToggle(rawId, !isSelected);
                }}
                className={`flex items-start gap-3 px-4 py-3 transition-colors select-none ${isLocked ? "opacity-60 cursor-not-allowed" : "cursor-pointer hover:bg-zinc-900/60"} ${isSelected ? "bg-zinc-900/40" : ""}`}
              >
                <div
                  className={`mt-0.5 h-4 w-4 flex-shrink-0 flex items-center justify-center border transition-colors ${
                    isSelected ? "bg-accent border-accent" : "bg-bg border-border-strong"
                  } rounded-md`}
                >
                  {isSelected && (
                    <svg className="h-2.5 w-2.5 text-accent-fg" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="2,6 5,9 10,3" />
                    </svg>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-bold text-zinc-200">{getLabel(item)}</span>
                    {isLocked && (
                      <span className="text-[9px] uppercase font-bold px-1.5 py-0.5 bg-zinc-800 border border-zinc-700 text-text-faint tracking-wider rounded-md">
                        auto
                      </span>
                    )}
                  </div>
                  {desc && <p className="text-xs text-text-faint mt-0.5 line-clamp-1">{desc}</p>}
                  <p className="text-[10px] text-text-faint font-mono mt-0.5">{getId(item)}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
