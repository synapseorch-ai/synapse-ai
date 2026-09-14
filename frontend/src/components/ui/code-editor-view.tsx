"use client";

/**
 * The CodeMirror half of `CodeEditor`. **The only file in the app that imports
 * CodeMirror** — `code-editor.tsx` loads it with `next/dynamic`, so the editor
 * is a chunk fetched by the pages that show one, not a weight carried by every
 * page that imports the kit.
 *
 * **Themed from CSS variables and nothing else.** The two products switch
 * themes differently (a `.light-mode` class here, `data-theme="light"` in
 * cloud), so the editor never asks which theme is on: every colour is a token,
 * and the tokens change underneath it. No hex values, no `darkTheme` facet, no
 * reconfigure on a theme switch. The editor used to be one-dark in both themes
 * on purpose; a dark block in a light form is what the light theme was built
 * to stop.
 *
 * Values from outside are applied with an annotation the change listener
 * ignores, so re-rendering with the value the editor just reported does not
 * report it again, and they stay out of the undo history. A caller showing a
 * different document should also change the `key`, so undo can never walk back
 * into the previous one.
 */

import { completionStatus, startCompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { linter } from "@codemirror/lint";
import { Annotation, Compartment, EditorState, Prec, Transaction, type Extension } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  keymap,
  placeholder as placeholderText,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { basicSetup } from "codemirror";
import * as React from "react";

import { formatJson, matchTemplate, type TemplateProfile } from "./template-complete";
import { useTemplateSources } from "./template-context";

export type CodeLanguage = "json" | "python" | "text";

export type CodeEditorController = { format: () => boolean };

export type CodeEditorViewProps = {
  value: string;
  onChange?: (value: string) => void;
  language: CodeLanguage;
  profile?: TemplateProfile;
  /** Positioned JSON parse errors. A template body is checked by the frame instead. */
  lintJson?: boolean;
  readOnly?: boolean;
  placeholder?: string;
  controller?: React.RefObject<CodeEditorController | null>;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
};

const External = Annotation.define<boolean>();

const theme = EditorView.theme({
  "&": { height: "100%", backgroundColor: "var(--surface)", color: "var(--text)", fontSize: "12px" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "inherit", lineHeight: "1.6", overflow: "auto" },
  ".cm-content": { padding: "8px 0", caretColor: "var(--accent)" },
  ".cm-line": { padding: "0 12px" },
  ".cm-gutters": {
    backgroundColor: "var(--surface-2)",
    color: "var(--text-faint)",
    border: "none",
    borderRight: "1px solid var(--border)",
  },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text-muted)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--surface-2) 55%, transparent)" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--accent)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: "color-mix(in srgb, var(--accent) 24%, transparent)" },
  ".cm-selectionMatch": { backgroundColor: "color-mix(in srgb, var(--accent) 12%, transparent)" },
  "&.cm-focused .cm-matchingBracket": { backgroundColor: "var(--accent-subtle)", color: "inherit", outline: "none" },
  ".cm-placeholder": { color: "var(--text-faint)" },
  ".cm-foldPlaceholder": { backgroundColor: "var(--surface-2)", border: "none", color: "var(--text-muted)" },
  ".cm-panels": { backgroundColor: "var(--surface-2)", color: "var(--text)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
  ".cm-textfield": {
    backgroundColor: "var(--surface)",
    border: "1px solid var(--border-strong)",
    borderRadius: "4px",
    color: "var(--text)",
  },
  ".cm-button": {
    backgroundImage: "none",
    backgroundColor: "var(--surface)",
    border: "1px solid var(--border-strong)",
    borderRadius: "4px",
    color: "var(--text)",
  },
  ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, var(--warning) 25%, transparent)", outline: "none" },
  ".cm-tooltip": {
    backgroundColor: "var(--surface)",
    color: "var(--text)",
    border: "1px solid var(--border-strong)",
    borderRadius: "8px",
    overflow: "hidden",
  },
  // The autocomplete base theme styles these through `.cm-tooltip.cm-tooltip-autocomplete`
  // and a light/dark marker class, so equal selectors lose to it: its 1px padding,
  // its generic monospace and its fixed blue won. `&.cm-editor` outranks both.
  "&.cm-editor .cm-tooltip.cm-tooltip-autocomplete > ul": {
    fontFamily: "inherit",
    maxHeight: "16rem",
    padding: "4px",
  },
  "&.cm-editor .cm-tooltip.cm-tooltip-autocomplete > ul > li": {
    padding: "4px 8px",
    lineHeight: "1.5",
    borderRadius: "6px",
  },
  "&.cm-editor .cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "var(--accent-subtle)",
    color: "var(--text)",
  },
  ".cm-completionIcon": { display: "none" },
  ".cm-completionDetail": { color: "var(--text-faint)", fontStyle: "normal", marginLeft: "1.5em" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "var(--accent)" },
  // The default squiggle is an SVG with its colour baked in, which no token can reach.
  ".cm-lintRange-error": { backgroundImage: "none", textDecoration: "underline wavy var(--danger)", textUnderlineOffset: "3px" },
  ".cm-lintRange-warning": { backgroundImage: "none", textDecoration: "underline wavy var(--warning)", textUnderlineOffset: "3px" },
  ".cm-diagnostic": { padding: "4px 8px", borderLeft: "3px solid var(--border-strong)" },
  ".cm-diagnostic-error": { borderLeftColor: "var(--danger)" },
  ".cm-diagnostic-warning": { borderLeftColor: "var(--warning)" },
  ".cm-template-ref": { color: "var(--accent)", backgroundColor: "var(--accent-subtle)", borderRadius: "3px" },
});

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.definitionKeyword, tags.moduleKeyword, tags.operatorKeyword], color: "var(--accent)" },
  { tag: tags.propertyName, color: "var(--accent)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--success)" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: "var(--warning)" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: "var(--text-faint)", fontStyle: "italic" },
  { tag: [tags.definition(tags.function(tags.variableName)), tags.className], fontWeight: "600" },
  { tag: [tags.self, tags.special(tags.variableName)], color: "var(--warning)" },
]);

/** `{{ name }}` stands out, so a reference reads as one rather than as punctuation. */
const templateRefs = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    matcher = new MatchDecorator({
      regexp: /\{\{[ \t]*[A-Za-z_][\w.-]*[ \t]*\}\}/g,
      decoration: Decoration.mark({ class: "cm-template-ref" }),
    });
    constructor(view: EditorView) {
      this.decorations = this.matcher.createDeco(view);
    }
    update(update: ViewUpdate) {
      this.decorations = this.matcher.updateDeco(update, this.decorations);
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

function languageExtension(language: CodeLanguage, lintJson: boolean): Extension {
  if (language === "python") return python();
  if (language === "json") return lintJson ? [json(), linter(jsonParseLinter(), { delay: 400 })] : json();
  return [];
}

function formatDocument(view: EditorView): boolean {
  const result = formatJson(view.state.doc.toString());
  if (!result.ok) return false;
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: result.text } });
  return true;
}

export function CodeEditorView({
  value,
  onChange,
  language,
  profile,
  lintJson = false,
  readOnly = false,
  placeholder,
  controller,
  ...aria
}: CodeEditorViewProps) {
  const container = React.useRef<HTMLDivElement>(null);
  const view = React.useRef<EditorView | null>(null);
  const compartments = React.useRef({
    language: new Compartment(),
    readOnly: new Compartment(),
    placeholder: new Compartment(),
    attributes: new Compartment(),
    refs: new Compartment(),
  });

  const { sources, load } = useTemplateSources();
  const live = React.useRef({ onChange, profile, sources, load });
  React.useLayoutEffect(() => {
    live.current = { onChange, profile, sources, load };
  });

  const ariaLabel = aria["aria-label"];
  const ariaLabelledBy = aria["aria-labelledby"];
  const ariaDescribedBy = aria["aria-describedby"];
  const ariaInvalid = aria["aria-invalid"];
  const attributes = React.useMemo(() => {
    const out: Record<string, string> = {};
    if (ariaLabel) out["aria-label"] = ariaLabel;
    if (ariaLabelledBy) out["aria-labelledby"] = ariaLabelledBy;
    if (ariaDescribedBy) out["aria-describedby"] = ariaDescribedBy;
    if (ariaInvalid) out["aria-invalid"] = "true";
    return out;
  }, [ariaLabel, ariaLabelledBy, ariaDescribedBy, ariaInvalid]);

  // Mount once. Everything that can change afterwards lives in a compartment or
  // in `live`, so the view — its history, its scroll, its cursor — survives it.
  React.useEffect(() => {
    if (!container.current) return;
    const c = compartments.current;

    const completeTemplates = (context: CompletionContext): CompletionResult | null => {
      const { profile: currentProfile, sources: currentSources, load: loadVariables } = live.current;
      if (!currentProfile) return null;
      const line = context.state.doc.lineAt(context.pos);
      const offset = context.pos - line.from;
      if (/\{\{[ \t]*[\w.-]*$/.test(line.text.slice(0, offset))) loadVariables();
      const match = matchTemplate(line.text, offset, currentProfile, currentSources);
      if (!match) return null;
      return {
        from: line.from + match.from,
        to: line.from + match.to,
        filter: false,
        options: match.candidates.map((candidate) => ({
          label: candidate.label,
          detail: candidate.detail,
          apply: candidate.insert,
        })),
      };
    };

    const created = new EditorView({
      parent: container.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          theme,
          syntaxHighlighting(highlight),
          Prec.high(keymap.of([{ key: "Mod-Shift-f", run: formatDocument }])),
          EditorState.languageData.of(() => [{ autocomplete: completeTemplates }]),
          c.language.of(languageExtension(language, lintJson)),
          c.readOnly.of(EditorState.readOnly.of(readOnly)),
          c.placeholder.of(placeholder ? placeholderText(placeholder) : []),
          c.attributes.of(EditorView.contentAttributes.of(attributes)),
          c.refs.of(profile ? templateRefs : []),
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            if (update.transactions.some((tr) => tr.annotation(External))) return;
            live.current.onChange?.(update.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = created;
    return () => {
      created.destroy();
      view.current = null;
    };
    // Mount-only by design; the effects below carry every later change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    const current = view.current;
    if (!current) return;
    const doc = current.state.doc.toString();
    if (doc === value) return;
    // The smallest change that turns the document into `value`, so the cursor
    // and scroll position survive a parent that normalises what it was given.
    let start = 0;
    while (start < doc.length && start < value.length && doc[start] === value[start]) start++;
    let endDoc = doc.length;
    let endValue = value.length;
    while (endDoc > start && endValue > start && doc[endDoc - 1] === value[endValue - 1]) {
      endDoc--;
      endValue--;
    }
    current.dispatch({
      changes: { from: start, to: endDoc, insert: value.slice(start, endValue) },
      annotations: [External.of(true), Transaction.addToHistory.of(false)],
    });
  }, [value]);

  React.useEffect(() => {
    const c = compartments.current;
    view.current?.dispatch({
      effects: [
        c.language.reconfigure(languageExtension(language, lintJson)),
        c.readOnly.reconfigure(EditorState.readOnly.of(readOnly)),
        c.placeholder.reconfigure(placeholder ? placeholderText(placeholder) : []),
        c.attributes.reconfigure(EditorView.contentAttributes.of(attributes)),
        c.refs.reconfigure(profile ? templateRefs : []),
      ],
    });
  }, [language, lintJson, readOnly, placeholder, attributes, profile]);

  // Variables arrive after the list first opens; show them without another keystroke.
  React.useEffect(() => {
    const current = view.current;
    if (current && completionStatus(current.state) !== null) startCompletion(current);
  }, [sources]);

  React.useImperativeHandle(controller, () => ({ format: () => (view.current ? formatDocument(view.current) : false) }), []);

  return <div ref={container} className="h-full" />;
}
