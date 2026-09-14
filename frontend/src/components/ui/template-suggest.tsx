"use client";

/**
 * The suggestion list under a text field — `{{` for template names, and an
 * optional trigger like `@` whose results come from somewhere else.
 *
 * One hook for `TemplateInput` and `MarkdownEditor`, so a textarea and an input
 * behave identically. CodeMirror has its own completion UI and does not use
 * this; it shares only `template-complete`.
 *
 * Choices worth knowing before changing this:
 *
 * - **Focus never leaves the field.** The list is a non-modal Radix popover
 *   that refuses auto-focus, options take no focus on mouse-down, and the
 *   active option is announced through `aria-activedescendant`. Typing keeps
 *   working while the list is open, which is the point of it.
 * - **Insertion goes through `execCommand("insertText")`**, which is deprecated
 *   and still the only way to change a textarea that keeps the browser's own
 *   undo stack. Where it is refused, `setRangeText` plus a synthetic `input`
 *   event reaches React's `onChange` the way typing does.
 * - **The list sits at the caret** in a textarea (measured with a mirror
 *   element) and under the field in an input, where the caret rarely moves far.
 * - **Escape hides it until the text changes**, not until the next keystroke
 *   anywhere — dismissing and then moving the cursor should not bring it back.
 */

import * as RPopover from "@radix-ui/react-popover";
import * as React from "react";

import { cn } from "@/lib/cn";

import { applyTemplate, matchMention, matchTemplate, type TemplateProfile } from "./template-complete";
import { useTemplateSources } from "./template-context";

export type MentionItem = { id: string; label: string; detail?: string; insert: string };

/** A second trigger with results from elsewhere — the vault's `@`, for one. */
export type MentionSource = {
  trigger: string;
  search: (query: string) => Promise<MentionItem[]>;
};

type Field = HTMLTextAreaElement | HTMLInputElement;

type Item = { id: string; label: string; detail?: string; from: number; to: number; insert: string };

const MENTION_DEBOUNCE_MS = 150;

/** Replace `[from, to)` in a controlled field so React and the undo stack both see it. */
export function replaceInField(field: Field, from: number, to: number, text: string, select?: [number, number]) {
  field.focus();
  field.setSelectionRange(from, to);
  const inserted = typeof document.execCommand === "function" && document.execCommand("insertText", false, text);
  if (!inserted) {
    field.setRangeText(text, from, to, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (select) field.setSelectionRange(select[0], select[1]);
}

const MIRRORED = [
  "boxSizing", "width", "height", "overflowX", "overflowY",
  "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "fontStyle", "fontVariant", "fontWeight", "fontStretch", "fontSize", "lineHeight", "fontFamily",
  "textAlign", "textTransform", "textIndent", "letterSpacing", "wordSpacing", "tabSize",
] as const;

/** The caret's box in viewport coordinates. */
function caretRect(field: Field, position: number): DOMRect {
  const box = field.getBoundingClientRect();
  if (field instanceof HTMLInputElement) return new DOMRect(box.left, box.top, box.width, box.height);

  const style = window.getComputedStyle(field);
  const mirror = document.createElement("div");
  for (const property of MIRRORED) mirror.style[property] = style[property];
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.textContent = field.value.slice(0, position);
  const marker = document.createElement("span");
  marker.textContent = field.value.slice(position) || ".";
  mirror.appendChild(marker);
  document.body.appendChild(mirror);
  const top = marker.offsetTop + parseFloat(style.borderTopWidth) - field.scrollTop;
  const left = marker.offsetLeft + parseFloat(style.borderLeftWidth) - field.scrollLeft;
  const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4;
  document.body.removeChild(mirror);
  return new DOMRect(
    box.left + Math.min(Math.max(left, 0), box.width),
    box.top + Math.min(Math.max(top, 0), box.height - lineHeight),
    1,
    lineHeight,
  );
}

export function useSuggestions<T extends Field>({
  ref,
  value,
  profile,
  mentions,
  disabled,
}: {
  ref: React.RefObject<T | null>;
  value: string;
  profile?: TemplateProfile;
  mentions?: MentionSource;
  disabled?: boolean;
}) {
  const listId = React.useId();
  const { sources, load } = useTemplateSources();
  const [cursor, setCursor] = React.useState<number | null>(null);
  const [active, setActive] = React.useState<{ key: string | null; index: number }>({ key: null, index: 0 });
  const [dismissed, setDismissed] = React.useState<string | null>(null);
  const [mentionResults, setMentionResults] = React.useState<{ query: string; items: MentionItem[] } | null>(null);

  const at = cursor === null ? null : Math.min(cursor, value.length);

  const template = React.useMemo(
    () => (profile && at !== null ? matchTemplate(value, at, profile, sources) : null),
    [profile, at, value, sources],
  );

  const mention = React.useMemo(
    () => (mentions && at !== null && !template ? matchMention(value, at, mentions.trigger) : null),
    [mentions, at, value, template],
  );

  React.useEffect(() => {
    if (!mention || !mentions) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      mentions.search(mention.query).then(
        (items) => !cancelled && setMentionResults({ query: mention.query, items }),
        () => !cancelled && setMentionResults({ query: mention.query, items: [] }),
      );
    }, MENTION_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [mention, mentions]);

  const items: Item[] = React.useMemo(() => {
    if (template)
      return template.candidates.map((c) => ({
        id: `${c.kind}:${c.label}`,
        label: c.label,
        detail: c.detail,
        from: template.from,
        to: template.to,
        insert: c.insert,
      }));
    if (mention && mentionResults?.query === mention.query)
      return mentionResults.items.map((m) => ({ ...m, from: mention.from, to: mention.to }));
    return [];
  }, [template, mention, mentionResults]);

  const token = template ?? mention;
  const tokenKey = token ? `${token.from}:${value}` : null;
  const open = !disabled && items.length > 0 && tokenKey !== dismissed;

  // Variables are fetched when a `{{` first needs them, not when the field mounts.
  const wantsGlobals = Boolean(profile && at !== null && /\{\{[ \t]*[\w.-]*$/.test(value.slice(0, at)));
  React.useEffect(() => {
    if (wantsGlobals) load();
  }, [wantsGlobals, load]);

  // A new query starts at the top of its list rather than wherever the last one was.
  const queryKey = token ? `${token.from}:${token.query}` : null;
  const activeIndex = active.key === queryKey ? Math.min(active.index, Math.max(items.length - 1, 0)) : 0;
  const setActiveIndex = (index: number) => setActive({ key: queryKey, index });

  const choose = React.useCallback(
    (item: Item) => {
      const field = ref.current;
      if (!field) return;
      const { cursor: next } = applyTemplate(value, item, item);
      replaceInField(field, item.from, item.to, item.insert, [next, next]);
      setCursor(next);
    },
    [ref, value],
  );

  const track = (event: React.SyntheticEvent<T>) => {
    const field = event.currentTarget;
    setCursor(field.selectionStart === field.selectionEnd ? field.selectionStart : null);
  };

  const onKeyDown = (event: React.KeyboardEvent<T>) => {
    // Mid-composition, Enter belongs to the input method, not to this list.
    if (!open || event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((activeIndex + step + items.length) % items.length);
    } else if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey) {
      event.preventDefault();
      choose(items[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setDismissed(tokenKey);
    }
  };

  const anchor = React.useMemo(
    () => ({
      current: {
        getBoundingClientRect: () =>
          ref.current && at !== null ? caretRect(ref.current, token?.from ?? at) : new DOMRect(),
      },
    }),
    [ref, at, token?.from],
  );

  const optionId = (index: number) => `${listId}-option-${index}`;

  const list = (
    <RPopover.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) setDismissed(tokenKey);
      }}
    >
      <RPopover.Anchor virtualRef={anchor} />
      <RPopover.Portal>
        <RPopover.Content
          side="bottom"
          align="start"
          sideOffset={4}
          collisionPadding={8}
          hideWhenDetached
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onInteractOutside={(event) => {
            if (ref.current && event.target instanceof Node && ref.current.contains(event.target))
              event.preventDefault();
          }}
          className="z-50 w-72 max-w-[calc(100vw-1rem)] overflow-hidden rounded-lg border border-border-strong bg-surface shadow-xl"
        >
          <ul
            id={listId}
            role="listbox"
            aria-label="Suggestions"
            className="max-h-64 overflow-y-auto overscroll-contain p-1"
          >
            {items.map((item, index) => (
              <li
                key={item.id}
                id={optionId(index)}
                role="option"
                aria-selected={index === activeIndex}
                data-active={index === activeIndex}
                ref={(node) => {
                  if (node && index === activeIndex) node.scrollIntoView({ block: "nearest" });
                }}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(item)}
                className={cn(
                  "flex cursor-pointer select-none items-baseline justify-between gap-3 rounded-md px-2 py-1.5",
                  "data-[active=true]:bg-surface-2",
                )}
              >
                <span className="truncate font-code text-xs text-text">{item.label}</span>
                {item.detail && <span className="shrink-0 text-2xs text-text-faint">{item.detail}</span>}
              </li>
            ))}
          </ul>
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );

  return {
    open,
    list,
    onKeyDown,
    track,
    onBlur: () => setCursor(null),
    aria: {
      "aria-autocomplete": "list" as const,
      "aria-controls": open ? listId : undefined,
      "aria-activedescendant": open ? optionId(activeIndex) : undefined,
    },
  };
}
