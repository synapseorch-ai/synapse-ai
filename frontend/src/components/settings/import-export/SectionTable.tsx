"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Checkbox } from "@/components/ui";

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
    <div className="overflow-hidden rounded-lg border border-border">
      <button
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center gap-2 px-4 py-3 bg-surface-header hover:bg-row-hover transition-colors"
      >
        <Icon className="h-4 w-4 text-text-muted" />
        <span className="text-sm font-semibold text-text">{title}</span>
        <span className="ml-auto flex items-center gap-2 text-[10px] text-text-faint font-mono">
          {selectedCount}/{items.length}
          {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </span>
      </button>

      {!collapsed && (
        <div className="divide-y divide-border max-h-[400px] overflow-y-auto">
          {items.map((item, index) => {
            const id = getId(item) ?? `item-${index}`;
            const isSelected = selected.has(getId(item));
            const isLocked = locked.has(getId(item));
            const desc = getDesc?.(item);
            return (
              // A real checkbox inside a label: the row said role="checkbox" but
              // took no focus and no key, so a keyboard could pick nothing (F236).
              <label
                key={id || `fallback-${index}`}
                className={`flex items-start gap-3 px-4 py-3 transition-colors select-none ${isLocked ? "opacity-60 cursor-not-allowed" : "cursor-pointer hover:bg-row-hover"} ${isSelected ? "bg-surface-2" : ""}`}
              >
                <Checkbox
                  className="mt-0.5"
                  checked={isSelected}
                  disabled={isLocked}
                  label={getLabel(item)}
                  onChange={(checked) => {
                    const rawId = getId(item);
                    if (!isLocked && rawId) onToggle(rawId, checked);
                  }}
                />
                <span className="block flex-1 min-w-0">
                  <span className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-text">{getLabel(item)}</span>
                    {isLocked && (
                      <span className="text-[9px] uppercase font-semibold px-1.5 py-0.5 bg-surface-2 border border-border-strong text-text-faint tracking-wider rounded-md">
                        auto
                      </span>
                    )}
                  </span>
                  {desc && <span className="block text-xs text-text-faint mt-0.5 line-clamp-1">{desc}</span>}
                  <span className="block text-[10px] text-text-faint font-mono mt-0.5">{getId(item)}</span>
                </span>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
