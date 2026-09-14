"use client";

/**
 * A markdown field in the shape GitHub made familiar: Write and Preview tabs, a
 * small toolbar, Ctrl/⌘+B, I and K — for the prose an agent reads, its system
 * prompt, a skill's instructions, a step's prompt.
 *
 * A textarea, not CodeMirror, deliberately: this is writing, not code, and a
 * textarea brings spell-check, the platform's own text handling and undo for
 * free. Toolbar edits go through the same insert path as a suggestion
 * (`replaceInField`), so Ctrl+Z undoes a bold like it undoes typing. The
 * formatting itself is `markdown-format.ts`.
 *
 * The textarea stays mounted while Preview shows — hidden, not unmounted — so
 * switching back keeps its undo history and the caret where it was.
 *
 * `profile` turns on `{{ … }}` suggestions for what that field renders (see
 * `template-complete`); a field that renders nothing, like a skill body, passes
 * none. `mentions` adds a second trigger with its own source — the
 * self-hosted vault's `@`. `compact` is for the orchestration side panel.
 *
 * Inside a `Field`, pass `composite` to the field.
 */

import { Bold, Code, Heading, Italic, Link2, List, ListOrdered, Quote } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { controlStyles } from "./control-styles";
import { Markdown } from "./markdown";
import { insertLink, toggleLinePrefix, toggleWrap, type MarkdownEdit } from "./markdown-format";
import type { TemplateProfile } from "./template-complete";
import { replaceInField, useSuggestions, type MentionSource } from "./template-suggest";

export type MarkdownTab = "write" | "preview";

export type MarkdownEditorProps = {
  value: string;
  onChange: (value: string) => void;
  profile?: TemplateProfile;
  mentions?: MentionSource;
  compact?: boolean;
  rows?: number;
  placeholder?: string;
  disabled?: boolean;
  /** Controlled tab. Leave unset to start on `defaultTab` and let the editor keep its own. */
  tab?: MarkdownTab;
  defaultTab?: MarkdownTab;
  onTabChange?: (tab: MarkdownTab) => void;
  /** What Preview shows for an empty value. */
  previewEmpty?: React.ReactNode;
  id?: string;
  invalid?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
};

type Action = {
  key: string;
  label: string;
  shortcut?: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  run: (text: string, start: number, end: number) => MarkdownEdit;
};

const ACTIONS: Action[] = [
  { key: "heading", label: "Heading", icon: Heading, run: (t, s, e) => toggleLinePrefix(t, s, e, "heading") },
  { key: "bold", label: "Bold", shortcut: "B", icon: Bold, run: (t, s, e) => toggleWrap(t, s, e, "**", "bold text") },
  { key: "italic", label: "Italic", shortcut: "I", icon: Italic, run: (t, s, e) => toggleWrap(t, s, e, "_", "italic text") },
  { key: "code", label: "Code", icon: Code, run: (t, s, e) => toggleWrap(t, s, e, "`", "code") },
  { key: "link", label: "Link", shortcut: "K", icon: Link2, run: insertLink },
  { key: "bullet", label: "Bulleted list", icon: List, run: (t, s, e) => toggleLinePrefix(t, s, e, "bullet") },
  { key: "numbered", label: "Numbered list", icon: ListOrdered, run: (t, s, e) => toggleLinePrefix(t, s, e, "numbered") },
  { key: "quote", label: "Quote", icon: Quote, run: (t, s, e) => toggleLinePrefix(t, s, e, "quote") },
];

const NO_SUBSCRIBE = () => () => undefined;

const BY_SHORTCUT = new Map(ACTIONS.filter((a) => a.shortcut).map((a) => [a.shortcut!.toLowerCase(), a]));

export function MarkdownEditor({
  value,
  onChange,
  profile,
  mentions,
  compact = false,
  rows,
  placeholder,
  disabled = false,
  tab: controlledTab,
  defaultTab = "write",
  onTabChange,
  previewEmpty,
  id,
  invalid,
  className,
  ...aria
}: MarkdownEditorProps) {
  const baseId = React.useId();
  const textareaId = id ?? `${baseId}-text`;
  const ref = React.useRef<HTMLTextAreaElement>(null);
  const [ownTab, setOwnTab] = React.useState<MarkdownTab>(defaultTab);
  const tab = controlledTab ?? ownTab;
  // Preview takes the textarea's height when it opens, so the form does not jump.
  const [previewHeight, setPreviewHeight] = React.useState<number | undefined>(undefined);
  const suggest = useSuggestions({ ref, value, profile, mentions, disabled: disabled || tab !== "write" });

  // Which modifier the shortcut hints name. The server renders "Ctrl+".
  const isMac = React.useSyncExternalStore(
    NO_SUBSCRIBE,
    () => /Mac|iPhone|iPad/.test(navigator.platform),
    () => false,
  );

  const chooseTab = (next: MarkdownTab, focusText: boolean) => {
    if (next === tab) return;
    if (next === "preview" && ref.current) setPreviewHeight(ref.current.offsetHeight);
    if (controlledTab === undefined) setOwnTab(next);
    onTabChange?.(next);
    // A click on Write means "let me type"; arrowing across the tabs does not.
    if (next === "write" && focusText) window.requestAnimationFrame(() => ref.current?.focus());
  };

  const apply = (action: Action) => {
    const field = ref.current;
    if (!field || disabled) return;
    const edit = action.run(field.value, field.selectionStart, field.selectionEnd);
    replaceInField(field, edit.from, edit.to, edit.insert, [edit.selectionStart, edit.selectionEnd]);
  };

  const tabId = (which: MarkdownTab) => `${baseId}-tab-${which}`;
  const panelId = (which: MarkdownTab) => `${baseId}-panel-${which}`;
  const iconSize = compact ? "size-3.5" : "size-4";

  return (
    <div
      className={cn(
        "overflow-hidden rounded-md border bg-surface transition-colors focus-within:border-accent",
        invalid ? "border-danger" : "border-border-strong",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-2 px-1">
        <div
          role="tablist"
          aria-label="Editor mode"
          className="flex"
          onKeyDown={(event) => {
            if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
              event.preventDefault();
              const next = tab === "write" ? "preview" : "write";
              chooseTab(next, false);
              document.getElementById(tabId(next))?.focus();
            }
          }}
        >
          {(["write", "preview"] as const).map((which) => (
            <button
              key={which}
              id={tabId(which)}
              type="button"
              role="tab"
              aria-selected={tab === which}
              aria-controls={panelId(which)}
              tabIndex={tab === which ? 0 : -1}
              onClick={() => chooseTab(which, true)}
              className={cn(
                "-mb-px border-b-2 font-medium transition-colors",
                compact ? "px-2 py-1.5 text-xs" : "px-3 py-2 text-sm",
                tab === which
                  ? "border-accent text-text"
                  : "border-transparent text-text-faint hover:text-text",
              )}
            >
              {which === "write" ? "Write" : "Preview"}
            </button>
          ))}
        </div>

        {tab === "write" && !disabled && (
          <div role="toolbar" aria-label="Formatting" aria-controls={textareaId} className="flex items-center">
            {ACTIONS.map((action) => {
              const Icon = action.icon;
              const hint = action.shortcut ? `${action.label} (${isMac ? "⌘" : "Ctrl+"}${action.shortcut})` : action.label;
              return (
                <button
                  key={action.key}
                  type="button"
                  aria-label={action.label}
                  title={hint}
                  // Keep the textarea's focus and selection; the action reads both.
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => apply(action)}
                  className={cn(
                    "grid place-items-center rounded text-text-faint transition-colors hover:bg-surface hover:text-text",
                    compact ? "size-6" : "size-7",
                  )}
                >
                  <Icon className={iconSize} aria-hidden />
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div id={panelId("write")} role="tabpanel" aria-labelledby={tabId("write")} hidden={tab !== "write"}>
        <textarea
          {...suggest.aria}
          ref={ref}
          id={textareaId}
          value={value}
          rows={rows ?? (compact ? 5 : 14)}
          placeholder={placeholder}
          disabled={disabled}
          aria-label={aria["aria-label"]}
          aria-labelledby={aria["aria-labelledby"]}
          aria-describedby={aria["aria-describedby"]}
          aria-invalid={invalid || undefined}
          data-no-focus-ring
          className={cn(
            controlStyles({ size: compact ? "sm" : "md" }),
            "block resize-y rounded-none border-0 bg-transparent leading-relaxed focus-visible:ring-0",
          )}
          onChange={(event) => {
            suggest.track(event);
            onChange(event.target.value);
          }}
          onSelect={suggest.track}
          onBlur={suggest.onBlur}
          onKeyDown={(event) => {
            suggest.onKeyDown(event);
            if (event.defaultPrevented || event.nativeEvent.isComposing) return;
            // ⌘ on a Mac, where Ctrl+B already moves the caret back a character.
            if ((isMac ? event.metaKey : event.ctrlKey) && !event.shiftKey && !event.altKey) {
              const action = BY_SHORTCUT.get(event.key.toLowerCase());
              if (action) {
                event.preventDefault();
                apply(action);
              }
            }
          }}
        />
        {suggest.list}
      </div>

      <div
        id={panelId("preview")}
        role="tabpanel"
        aria-labelledby={tabId("preview")}
        hidden={tab !== "preview"}
        className={cn("overflow-y-auto", compact ? "p-2.5" : "p-4")}
        style={{ minHeight: previewHeight }}
      >
        {tab === "preview" &&
          (value.trim() ? (
            <Markdown className={compact ? "text-xs" : undefined}>{value}</Markdown>
          ) : (
            <p className="text-sm text-text-faint">{previewEmpty ?? "Nothing to preview."}</p>
          ))}
      </div>
    </div>
  );
}
