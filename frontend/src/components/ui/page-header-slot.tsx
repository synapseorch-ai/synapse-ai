"use client";

/**
 * Something the shell puts first in every page header's right-hand cluster.
 *
 * The help "?" is the one thing that goes there (H1): it belongs to the shell,
 * not to any page, yet it has to sit in each page's own header because neither
 * product has a bar of its own above the page. A slot lets the shell fill it
 * once, and `PageHeader` stays what it is — a kit primitive that knows nothing
 * about help, or about which product it is in.
 *
 * Its own client module so `PageHeader`, `Screen` and the rest of `text.tsx`
 * stay usable from a server page: a context needs a client component, and only
 * this one reads it. With no provider the slot is empty, which is how the
 * kitchen sink and anything outside the shell render.
 */

import * as React from "react";

const PageHeaderSlotContext = React.createContext<React.ReactNode>(null);

export function PageHeaderSlotProvider({ slot, children }: { slot: React.ReactNode; children: React.ReactNode }) {
  return <PageHeaderSlotContext.Provider value={slot}>{children}</PageHeaderSlotContext.Provider>;
}

/** Renders whatever the shell put in the slot, or nothing. */
export function PageHeaderSlot() {
  return <>{React.useContext(PageHeaderSlotContext)}</>;
}
