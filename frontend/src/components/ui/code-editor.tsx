"use client";

/**
 * A code editor — JSON, Python or plain text — for the fields that hold code:
 * a tool's input schema and request body, a Python tool, a transform step.
 *
 * This file is the frame: the border, the height, the status line and the
 * Format action. CodeMirror itself is `code-editor-view.tsx`, loaded with
 * `next/dynamic` so it is only downloaded where an editor is actually shown.
 * The frame owns the height so the page does not jump when it arrives.
 *
 * Inside a `Field`, pass `composite` to the field: the Format button would
 * otherwise become the label's control.
 *
 * `profile` turns on `{{ … }}` suggestions — see `template-complete`.
 */

import dynamic from "next/dynamic";
import * as React from "react";

import { cn } from "@/lib/cn";

import { formatJson, templateJsonProblem, type TemplateProfile } from "./template-complete";
import type { CodeEditorController, CodeLanguage } from "./code-editor-view";

export type { CodeLanguage } from "./code-editor-view";

const CodeEditorView = dynamic(() => import("./code-editor-view").then((m) => m.CodeEditorView), {
  ssr: false,
  loading: () => <div aria-hidden className="h-full animate-pulse bg-surface-2/40" />,
});

export type CodeEditorProps = {
  value: string;
  onChange?: (value: string) => void;
  language?: CodeLanguage;
  profile?: TemplateProfile;
  /**
   * `json` marks parse errors in place and says so underneath. `json-template`
   * is for a request body: references are allowed, and the line underneath says
   * when the result will be sent as text instead of JSON.
   */
  lint?: "json" | "json-template";
  /** A Format button (and Mod-Shift-F) for JSON. */
  format?: boolean;
  readOnly?: boolean;
  /** Pixels. The frame can be dragged taller. */
  height?: number;
  placeholder?: string;
  invalid?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
};

export function CodeEditor({
  value,
  onChange,
  language = "text",
  profile,
  lint,
  format = false,
  readOnly = false,
  height = 192,
  placeholder,
  invalid,
  className,
  ...aria
}: CodeEditorProps) {
  const controller = React.useRef<CodeEditorController | null>(null);

  const parsed = lint === "json" || format ? formatJson(value) : null;
  const problem =
    lint === "json" && value.trim() && parsed && !parsed.ok
      ? { tone: "danger" as const, text: `Not valid JSON — ${parsed.error}` }
      : lint === "json-template"
        ? (() => {
            const message = templateJsonProblem(value);
            return message ? { tone: "warning" as const, text: message } : null;
          })()
        : null;
  const canFormat = format && !readOnly && value.trim() !== "" && parsed?.ok === true && parsed.text !== `${value.trimEnd()}\n`;
  const footer = format || lint;

  return (
    <div
      className={cn(
        "flex resize-y flex-col overflow-hidden rounded-md border bg-surface font-code transition-colors focus-within:border-accent",
        invalid || problem?.tone === "danger" ? "border-danger" : "border-border-strong",
        className,
      )}
      style={{ height, minHeight: Math.min(height, 72) }}
    >
      <div className="min-h-0 flex-1">
        <CodeEditorView
          value={value}
          onChange={onChange}
          language={language}
          profile={profile}
          lintJson={lint === "json"}
          readOnly={readOnly}
          placeholder={placeholder}
          controller={controller}
          aria-label={aria["aria-label"]}
          aria-labelledby={aria["aria-labelledby"]}
          aria-describedby={aria["aria-describedby"]}
          aria-invalid={invalid || problem?.tone === "danger"}
        />
      </div>
      {footer && (
        <div className="flex min-h-8 shrink-0 items-center justify-between gap-3 border-t border-border bg-surface-2 px-3 py-1 font-sans text-xs">
          <span
            className={cn(
              "min-w-0 truncate",
              problem?.tone === "danger" ? "text-danger" : problem ? "text-warning" : "text-text-faint",
            )}
            role={problem ? "status" : undefined}
            title={problem?.text}
          >
            {problem?.text ?? (language === "json" ? "JSON" : "")}
          </span>
          {format && (
            <button
              type="button"
              onClick={() => controller.current?.format()}
              disabled={!canFormat}
              title="Format (Ctrl+Shift+F)"
              className="shrink-0 rounded px-2 py-0.5 font-medium text-text-muted transition-colors hover:bg-surface hover:text-text disabled:pointer-events-none disabled:opacity-45"
            >
              Format
            </button>
          )}
        </div>
      )}
    </div>
  );
}
