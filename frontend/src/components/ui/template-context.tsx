"use client";

/**
 * Where `{{ … }}` suggestions get their names.
 *
 * Three sources with three owners. **Workspace variables** belong to the
 * product — each one mounts `TemplateSourcesProvider` once, near the root, with
 * a loader for its own variables endpoint, because the kit knows no URLs.
 * **Arguments** belong to the tool form (its input schema) and **state keys** to
 * the orchestration panel, so those arrive through a nested `TemplateScope`
 * close to the field.
 *
 * Variables load on first use, not on mount: most screens that mount the
 * provider never open a suggestion. A loaded list is reused for thirty seconds
 * and then fetched again on the next suggestion, so a variable added in another
 * tab turns up without a reload. A loader must return names and a secret flag
 * only; the type has no room for a value, deliberately.
 *
 * With no provider mounted the editors still work — they simply have no
 * variables to offer.
 */

import * as React from "react";

import { normaliseVariables, type TemplateSources, type TemplateVariable } from "./template-complete";

const STALE_AFTER_MS = 30_000;

type Context = {
  globals: readonly TemplateVariable[] | null;
  loadGlobals: () => void;
  args: readonly string[];
  state: readonly string[];
};

const EMPTY: readonly string[] = [];

const TemplateSourcesContext = React.createContext<Context>({
  globals: null,
  loadGlobals: () => undefined,
  args: EMPTY,
  state: EMPTY,
});

export function TemplateSourcesProvider({
  loadGlobals,
  children,
}: {
  loadGlobals: () => Promise<readonly { key: string; secret?: boolean }[]>;
  children: React.ReactNode;
}) {
  const [globals, setGlobals] = React.useState<readonly TemplateVariable[] | null>(null);
  const inflight = React.useRef(false);
  const loadedAt = React.useRef(0);
  const loader = React.useRef(loadGlobals);

  React.useEffect(() => {
    loader.current = loadGlobals;
  }, [loadGlobals]);

  const load = React.useCallback(() => {
    if (inflight.current || Date.now() - loadedAt.current < STALE_AFTER_MS) return;
    inflight.current = true;
    loader
      .current()
      .then(
        (next) => {
          loadedAt.current = Date.now();
          setGlobals(normaliseVariables(next));
        },
        // A failed load offers no variables rather than an error in the middle
        // of someone typing; the next suggestion tries again.
        () => setGlobals((current) => current ?? []),
      )
      .finally(() => {
        inflight.current = false;
      });
  }, []);

  const value = React.useMemo<Context>(
    () => ({ globals, loadGlobals: load, args: EMPTY, state: EMPTY }),
    [globals, load],
  );

  return <TemplateSourcesContext.Provider value={value}>{children}</TemplateSourcesContext.Provider>;
}

/** Adds a form's own names. Each one given replaces the outer scope's; the rest pass through. */
export function TemplateScope({
  args,
  state,
  children,
}: {
  args?: readonly string[];
  state?: readonly string[];
  children: React.ReactNode;
}) {
  const outer = React.useContext(TemplateSourcesContext);
  const value = React.useMemo<Context>(
    () => ({ ...outer, args: args ?? outer.args, state: state ?? outer.state }),
    [outer, args, state],
  );
  return <TemplateSourcesContext.Provider value={value}>{children}</TemplateSourcesContext.Provider>;
}

/**
 * The names in reach of this field. `load` fetches variables if they are
 * missing or stale — call it when a suggestion is about to show, not on render.
 */
export function useTemplateSources(): { sources: TemplateSources; load: () => void } {
  const { globals, loadGlobals, args, state } = React.useContext(TemplateSourcesContext);
  const sources = React.useMemo<TemplateSources>(() => ({ globals, args, state }), [globals, args, state]);
  return { sources, load: loadGlobals };
}
