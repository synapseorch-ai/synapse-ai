"use client";

/*
 * Alt+T, Alt+W and Alt+1…9 for a tabbed page (TABS1, F923). Ctrl/Cmd+T and
 * Ctrl+W belong to the browser. A key held for a dialog or a menu is that
 * layer's, so nothing fires while one is open.
 */

import * as React from "react";

import { shortcutOf, type TabShortcut } from "./model";

export function useTabShortcuts(on: (shortcut: TabShortcut) => void, enabled = true) {
  const latest = React.useRef(on);
  React.useEffect(() => { latest.current = on; });
  React.useEffect(() => {
    if (!enabled) return;
    const listen = (e: KeyboardEvent) => {
      const shortcut = shortcutOf(e);
      if (!shortcut) return;
      if (document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]')) return;
      e.preventDefault();
      latest.current(shortcut);
    };
    window.addEventListener("keydown", listen);
    return () => window.removeEventListener("keydown", listen);
  }, [enabled]);
}
