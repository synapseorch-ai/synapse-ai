"use client";

/**
 * Canvas mode: while a canvas is open, the canvas is the page (D204, M3a).
 *
 * The workflow editor's open canvas fills the inset sheet: no page header, and
 * the menu folded to icons. The canvas lives deep in a page and the header and
 * the rail belong to the shell, so the canvas says "I'm open" here and the
 * shell reads it — the same shape as `page-header-slot`. A count, not a flag,
 * so two canvases mounting and unmounting in either order still leave it right.
 *
 * With no provider, nothing hides and nothing folds: a product that has not
 * wired it (cloud until M3b, the kitchen sink) renders as before.
 */

import * as React from "react";

type CanvasModeValue = { active: boolean; enter: () => () => void };

const CanvasModeContext = React.createContext<CanvasModeValue | null>(null);

export function CanvasModeProvider({ children }: { children: React.ReactNode }) {
  const [count, setCount] = React.useState(0);
  const enter = React.useCallback(() => {
    setCount((n) => n + 1);
    return () => setCount((n) => Math.max(0, n - 1));
  }, []);
  const value = React.useMemo(() => ({ active: count > 0, enter }), [count, enter]);
  return <CanvasModeContext.Provider value={value}>{children}</CanvasModeContext.Provider>;
}

/** Canvas side: the shell is in canvas mode for as long as this is mounted with `active`. */
export function useCanvasMode(active: boolean) {
  const enter = React.useContext(CanvasModeContext)?.enter;
  React.useEffect(() => {
    if (!active || !enter) return;
    return enter();
  }, [active, enter]);
}

/** Shell side: whether a canvas is open. */
export function useIsCanvasMode(): boolean {
  return React.useContext(CanvasModeContext)?.active ?? false;
}

/** Renders its children except while a canvas is open (the page header). */
export function CanvasModeHide({ children }: { children: React.ReactNode }) {
  return useIsCanvasMode() ? null : <>{children}</>;
}
