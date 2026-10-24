"use client";

/*
 * The new tab's agent picker (TABS1, F923): "+" opens it, Synapse AI first,
 * then every agent; picking one opens a tab with that agent. Nothing runs until
 * the person sends.
 *
 * A Popover with `cmdk`, the chat composer's agent picker's shape: a dropdown
 * menu owns every keystroke for its own type-ahead, so a filter box cannot live
 * in one. Its open state is the caller's, so Alt+T opens it as "+" does.
 */

import * as RPopover from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Plus, Search } from "lucide-react";
import * as React from "react";

import { AppMark, Mark, useOwnScroll } from "@/components/ui";
import { cn } from "@/lib/cn";

export interface PickableAgent {
  id: string;
  name: string;
  /** One faint line: what it is or does. */
  note?: string | null;
  /** The product's own assistant, listed first (Synapse AI). */
  assistant?: boolean;
}

export function NewTabMenu({
  agents,
  onPick,
  open,
  onOpenChange,
  label = "New tab",
  tour,
  first,
  more,
  heading = "Agents",
  listLabel = "Start a conversation with",
  placeholder = "Search agents…",
  footer = "Opens beside your other tabs. Nothing runs until you send.",
  className,
}: {
  agents: PickableAgent[];
  onPick: (agentId: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  label?: string;
  /** The guided tour's anchors (`data-tour`) on the button and on the list. */
  tour?: { button: string; list: string };
  /** A row above the list, its own action: the workflows page's "New workflow". */
  first?: { label: string; icon: React.ReactNode; onPick: () => void };
  /** A second group under the list: the workflows page's recent runs. */
  more?: { heading: string; items: PickableAgent[]; onPick: (id: string) => void };
  heading?: string;
  listLabel?: string;
  placeholder?: string;
  footer?: string;
  className?: string;
}) {
  const ownScroll = useOwnScroll<HTMLDivElement>();
  const assistants = agents.filter((a) => a.assistant);
  const others = agents.filter((a) => !a.assistant);
  const pick = (id: string) => { onOpenChange(false); onPick(id); };

  return (
    <RPopover.Root open={open} onOpenChange={onOpenChange}>
      <RPopover.Trigger
        type="button"
        aria-label={label}
        title={`${label} (Alt+T)`}
        data-tour={tour?.button}
        className={cn(
          "grid size-8 place-items-center rounded-md text-text-faint hover:bg-surface-2 hover:text-text data-[state=open]:bg-surface-2 data-[state=open]:text-text",
          className,
        )}
      >
        <Plus className="size-4" aria-hidden />
      </RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content
          align="start"
          sideOffset={6}
          collisionPadding={8}
          aria-label={listLabel}
          data-tour={tour?.list}
          className="z-50 flex w-72 max-w-[calc(100vw-16px)] max-h-[min(28rem,var(--radix-popover-content-available-height))] flex-col overflow-hidden rounded-lg border border-border-strong bg-surface shadow-elevated"
        >
          <Command loop className="flex min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-border px-3">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <Command.Input
                autoFocus
                placeholder={placeholder}
                aria-label={placeholder.replace(/…$/, "")}
                data-no-focus-ring
                className="h-9 w-full bg-transparent text-sm text-text outline-none placeholder:text-text-faint"
              />
            </div>
            <Command.List ref={ownScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1">
              <Command.Empty className="px-2 py-6 text-center text-sm text-text-faint">
                {agents.length === 0 ? "No agents yet." : "No agent matches."}
              </Command.Empty>
              {first && (
                <Command.Group>
                  <Command.Item
                    value={first.label}
                    onSelect={() => { onOpenChange(false); first.onPick(); }}
                    className="flex cursor-pointer select-none items-center gap-2.5 rounded-md px-2 py-1.5 text-sm font-medium text-accent-ink outline-none data-[selected=true]:bg-surface-2"
                  >
                    {first.icon}
                    {first.label}
                  </Command.Item>
                </Command.Group>
              )}
              {assistants.length > 0 && (
                <Command.Group>
                  {assistants.map((a) => <AgentRow key={a.id} agent={a} onPick={pick} />)}
                </Command.Group>
              )}
              {others.length > 0 && (
                <Command.Group
                  heading={heading}
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-text-faint"
                >
                  {others.map((a) => <AgentRow key={a.id} agent={a} onPick={pick} />)}
                </Command.Group>
              )}
              {more && more.items.length > 0 && (
                <Command.Group
                  heading={more.heading}
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-text-faint"
                >
                  {more.items.map((a) => (
                    <AgentRow key={a.id} agent={a} onPick={(id) => { onOpenChange(false); more.onPick(id); }} />
                  ))}
                </Command.Group>
              )}
            </Command.List>
          </Command>
          <p className="border-t border-border px-3 py-2 text-xs text-text-faint">
            {footer}
          </p>
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );
}

function AgentRow({ agent, onPick }: { agent: PickableAgent; onPick: (id: string) => void }) {
  return (
    <Command.Item
      value={`${agent.name} ${agent.id}`}
      onSelect={() => onPick(agent.id)}
      className="flex cursor-pointer select-none items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text outline-none data-[selected=true]:bg-surface-2"
    >
      {agent.assistant
        ? <Mark size={24} className="shrink-0 text-accent" />
        : <AppMark name={agent.name} seed={agent.id} size="sm" className="size-6 text-[11px]" />}
      <span className="flex min-w-0 flex-col">
        <span className="truncate">{agent.name}</span>
        {agent.note && <span className="truncate text-xs text-text-faint">{agent.note}</span>}
      </span>
    </Command.Item>
  );
}
