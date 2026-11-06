"use client";

/**
 * A conversation's scrollbar, shown only when it is wanted (F1414).
 *
 * A long conversation scrolls, and its bar stood at the pane's edge the whole
 * time, beside the messages and above the composer, as furniture. A pane with
 * the `scroll-quiet` class keeps its scrollbar's place but draws it clear until
 * the pointer is over the pane, the pane has keyboard focus, or the pane is
 * moving: this handler marks it `data-scrolling` on every scroll (wheel, keys,
 * touch, a drag of the thumb, the chat following its answer) and clears the mark
 * a second after the last. Each product's `globals.css` draws the three states.
 *
 * Only the bar is quiet: the pane scrolls exactly as before.
 *
 * Rule: a conversation's scrolling pane takes `scroll-quiet` and `onScroll={showWhileScrolling}`.
 */
import type * as React from "react";

const SHOWN_MS = 1000;
const settling = new WeakMap<HTMLElement, number>();

export function showWhileScrolling(event: React.UIEvent<HTMLElement>): void {
  const pane = event.currentTarget;
  pane.dataset.scrolling = "";
  window.clearTimeout(settling.get(pane));
  settling.set(pane, window.setTimeout(() => { delete pane.dataset.scrolling; }, SHOWN_MS));
}
