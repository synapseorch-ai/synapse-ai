"use client";

/**
 * A popup's list that scrolls itself, wherever the popup opens (F700).
 *
 * A Modal or a Drawer locks the page's scroll and lets only its own content
 * scroll: Radix Dialog's `react-remove-scroll` listens for `wheel` and
 * `touchmove` on the document and cancels any whose target is outside the
 * dialog. A non-modal popup (the Combobox's list, a template field's
 * suggestions) is portalled to `<body>`, outside the dialog, so the lock
 * cancelled every wheel and swipe over it: the Evaluate dialog's judge model
 * could be reached only with the arrow keys. `Select` and `Menu` are modal and
 * push a lock of their own, which lets their content scroll.
 *
 * So the list stops those two events before they reach the document, and the
 * browser scrolls it as it would anywhere. It has to be a native listener:
 * React's own sit on the root the App Router hydrates, the document itself, so a
 * React `onWheel` runs beside the lock, never before it. Outside a dialog
 * nothing on the document listens for them, so nothing changes there.
 *
 * Rule: a non-modal popup that scrolls takes this ref on the element that scrolls.
 */
import * as React from "react";

const stop = (event: Event) => event.stopPropagation();

export function useOwnScroll<T extends HTMLElement>(): React.RefCallback<T> {
  const attached = React.useRef<T | null>(null);
  return React.useCallback((node: T | null) => {
    attached.current?.removeEventListener("wheel", stop);
    attached.current?.removeEventListener("touchmove", stop);
    attached.current = node;
    node?.addEventListener("wheel", stop, { passive: true });
    node?.addEventListener("touchmove", stop, { passive: true });
  }, []);
}
