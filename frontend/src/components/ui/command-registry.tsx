"use client";

/**
 * A page's own group in ⌘K (M3a).
 *
 * ⌘K's list is the product's (`AppCommands` in OSS, `OrgCommands` in cloud):
 * pages, actions, things by name. A screen can add a group of its own while it
 * is open — the open canvas adds "Add a step" — and the product's list puts the
 * registered groups first. With no provider, registering does nothing.
 *
 * A group is registered again only when what it shows changes (its heading and
 * its items' ids, labels and hints). Its handlers are read through a ref, so a
 * page re-rendering on every keystroke does not churn the list, and a picked
 * item always runs the page's current handler.
 */

import * as React from "react";

import type { CommandGroup } from "./command-palette";

type Registry = { set: (id: string, group: CommandGroup | null) => void };

const RegistryContext = React.createContext<Registry | null>(null);
const GroupsContext = React.createContext<CommandGroup[]>([]);

export function CommandRegistryProvider({ children }: { children: React.ReactNode }) {
  const [byId, setById] = React.useState<Record<string, CommandGroup>>({});
  const set = React.useCallback((id: string, group: CommandGroup | null) => {
    setById((current) => {
      if (group) return { ...current, [id]: group };
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);
  const registry = React.useMemo(() => ({ set }), [set]);
  const groups = React.useMemo(() => Object.values(byId), [byId]);
  return (
    <RegistryContext.Provider value={registry}>
      <GroupsContext.Provider value={groups}>{children}</GroupsContext.Provider>
    </RegistryContext.Provider>
  );
}

/** Page side: show `group` in ⌘K for as long as this is mounted (`null` shows nothing). */
export function useRegisterCommands(id: string, group: CommandGroup | null) {
  const set = React.useContext(RegistryContext)?.set;
  const latest = React.useRef(group);
  React.useEffect(() => {
    latest.current = group;
  });
  const signature = group
    ? [group.heading, ...group.items.map((item) => `${item.id}\u0001${item.label}\u0001${item.hint ?? ""}`)].join("\u0000")
    : "";
  React.useEffect(() => {
    const current = latest.current;
    if (!set || !current) return;
    set(id, {
      heading: current.heading,
      items: current.items.map((item) => ({
        ...item,
        onSelect: () => latest.current?.items.find((x) => x.id === item.id)?.onSelect(),
      })),
    });
    return () => set(id, null);
    // `signature` stands for the group's contents; the handlers come through the ref.
  }, [set, id, signature]);
}

/** Product side: the groups pages have registered, in the order they arrived. */
export function useRegisteredCommands(): CommandGroup[] {
  return React.useContext(GroupsContext);
}
