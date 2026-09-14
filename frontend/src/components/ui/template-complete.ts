/**
 * `{{ … }}` suggestions — which names a field can use, and what typing one
 * inserts. Pure: text in, text out, so both editors (a textarea and CodeMirror)
 * share one answer and it is testable without a DOM.
 *
 * **What a field offers is what the engine renders there, and the engine does
 * not render the same thing everywhere.** Measured 2026-09-14 against
 * `core/templating.py` and its callers:
 *
 * | profile               | field                                   | rendered                                     |
 * |-----------------------|-----------------------------------------|----------------------------------------------|
 * | `tool-url`            | a custom tool's URL                     | `{{ arg }}`, `{{ args }}`, `{{ global.k }}`  |
 * | `tool`                | its headers and body                    | the same                                     |
 * | `step-prompt`         | agent / LLM / tool step prompt          | `{{ state.k }}`, `{{ global.k }}`, `{state.k}` |
 * | `legacy-state-nested` | print step content                      | `{state.k}`, `{state.k.path}` only           |
 * | `legacy-state`        | human step prompt                       | `{state.k}` only                             |
 * | `global`              | agent system prompt                     | `{{ global.k }}` only                        |
 *
 * A skill body renders nothing, so it has no profile. Offering a name a field
 * never renders would teach a syntax that silently stays literal.
 *
 * Details that are not style:
 *
 * - **Insertions are always spaced** — `{{ state.topic }}`. In a step prompt
 *   the older single-brace pass runs first and matches the *inside* of an
 *   unspaced `{{state.topic}}`, which then renders as `{gold}`.
 * - **A tool argument is offered bare** — `{{ invoice_id }}`, never
 *   `{{ args.invoice_id }}`. Both resolve, but in a URL only the bare form
 *   marks the argument as used, so the namespaced one is sent again as a query
 *   parameter.
 * - **A lone `{` opens nothing**, even where only the single-brace form works:
 *   prose and JSON are full of them, and an open list takes Enter. The list
 *   waits for the `s` of `state`.
 * - **A token never crosses a line.** The engine's pattern would; a suggestion
 *   list anchored three lines up would not help anyone.
 * - **A secret variable is a name and a flag, never a value.**
 *   `normaliseVariables` drops anything else a loader hands it.
 */

export type TemplateProfile =
  | "tool-url"
  | "tool"
  | "step-prompt"
  | "legacy-state"
  | "legacy-state-nested"
  | "global";

export type TemplateVariable = { key: string; secret: boolean };

export type TemplateSources = {
  /** Workspace variables — `null` while they have not loaded. */
  globals: readonly TemplateVariable[] | null;
  /** A tool's argument names, from its input schema — see `schemaArgs`. */
  args: readonly string[];
  /** An orchestration's state keys. */
  state: readonly string[];
};

export type TemplateCandidateKind = "arg" | "args" | "state" | "global";

export type TemplateCandidate = {
  kind: TemplateCandidateKind;
  /** What the list shows and what the query is matched against. */
  label: string;
  /** The text that replaces the token. */
  insert: string;
  detail: string;
};

export type TemplateMatch = {
  /** Where the token being typed starts — its first brace. */
  from: number;
  /** Where the replacement ends: past the rest of the name, and past a closing brace when there is one. */
  to: number;
  query: string;
  candidates: TemplateCandidate[];
};

/** The engine's name rule (`templating.REF`) without the braces, one segment at a time. */
const SEGMENT = /^[A-Za-z_][\w-]*$/;
/** `{state.key}` is `\w+` in every single-brace pass — no hyphen. */
const LEGACY_SEGMENT = /^\w+$/;
/** Only `args` is special to a bare lookup: `{{ args }}` is the whole payload, never an argument. */
const RESERVED_ARG = "args";

const DOUBLE_OPEN = /\{\{[ \t]*([A-Za-z_][\w.-]*)?$/;
const DOUBLE_CLOSE = /^[ \t]*\}\}/;
const LEGACY_QUERY = /^s(t(a(t(e(\.[\w.]*)?)?)?)?)?$/;
const WORD_TAIL = /^[\w-]*/;
const LEGACY_WORD_TAIL = /^\w*/;

const LIMIT = 100;

function doubleCandidates(profile: TemplateProfile, sources: TemplateSources): TemplateCandidate[] {
  const out: TemplateCandidate[] = [];
  if (profile === "tool" || profile === "tool-url") {
    for (const name of sources.args) {
      if (name === RESERVED_ARG || !name.split(".").every((part) => SEGMENT.test(part))) continue;
      out.push({ kind: "arg", label: name, insert: `{{ ${name} }}`, detail: "argument" });
    }
    out.push({ kind: "args", label: "args", insert: "{{ args }}", detail: "every argument, as JSON" });
  }
  if (profile === "step-prompt") {
    for (const key of sources.state) {
      if (!SEGMENT.test(key)) continue;
      out.push({ kind: "state", label: `state.${key}`, insert: `{{ state.${key} }}`, detail: "state" });
    }
  }
  if (profile === "tool" || profile === "tool-url" || profile === "step-prompt" || profile === "global") {
    for (const variable of sources.globals ?? []) {
      if (!SEGMENT.test(variable.key)) continue;
      out.push({
        kind: "global",
        label: `global.${variable.key}`,
        insert: `{{ global.${variable.key} }}`,
        detail: variable.secret ? "secret variable" : "variable",
      });
    }
  }
  return out;
}

function legacyCandidates(sources: TemplateSources): TemplateCandidate[] {
  return sources.state
    .filter((key) => LEGACY_SEGMENT.test(key))
    .map((key): TemplateCandidate => ({ kind: "state", label: `state.${key}`, insert: `{state.${key}}`, detail: "state" }));
}

/** Prefix matches first, then anything containing the query; source order within each. */
function rank(candidates: TemplateCandidate[], query: string, prefixOnly = false): TemplateCandidate[] {
  const q = query.toLowerCase();
  if (!q) return candidates.slice(0, LIMIT);
  const prefix: TemplateCandidate[] = [];
  const inside: TemplateCandidate[] = [];
  for (const candidate of candidates) {
    const label = candidate.label.toLowerCase();
    if (label.startsWith(q)) prefix.push(candidate);
    else if (!prefixOnly && label.includes(q)) inside.push(candidate);
  }
  return [...prefix, ...inside].slice(0, LIMIT);
}

/**
 * The token being typed at `cursor`, and what it could become — or `null` when
 * the cursor is not in one, or nothing matches.
 *
 * Editing inside a finished token works: `{{ glo|bal.key }}` replaces the whole
 * reference. A path after the name is kept — `{{ state.to|pic.name }}` keeps
 * `.name` — because only the part being typed is being chosen.
 */
export function matchTemplate(
  text: string,
  cursor: number,
  profile: TemplateProfile,
  sources: TemplateSources,
): TemplateMatch | null {
  const lineStart = text.lastIndexOf("\n", cursor - 1) + 1;
  const lineEnd = text.indexOf("\n", cursor);
  const before = text.slice(lineStart, cursor);
  const after = text.slice(cursor, lineEnd < 0 ? text.length : lineEnd);
  const legacyOnly = profile === "legacy-state" || profile === "legacy-state-nested";

  if (!legacyOnly) {
    const open = DOUBLE_OPEN.exec(before);
    if (open) {
      const query = open[1] ?? "";
      const candidates = rank(doubleCandidates(profile, sources), query);
      if (!candidates.length) return null;
      const word = WORD_TAIL.exec(after)?.[0] ?? "";
      const rest = after.slice(word.length);
      const close = DOUBLE_CLOSE.exec(rest)?.[0];
      // `.name` after the word stays, and so does the `}}` after it — the
      // choice then replaces only `{{ word`.
      const keepsPath = rest.startsWith(".");
      return {
        from: lineStart + open.index,
        to: cursor + word.length + (close && !keepsPath ? close.length : 0),
        query,
        candidates: keepsPath
          ? candidates.map((c) => ({ ...c, insert: c.insert.replace(/ \}\}$/, "") }))
          : candidates,
      };
    }
  }

  if (legacyOnly || profile === "step-prompt") {
    const brace = before.lastIndexOf("{");
    const query = brace < 0 ? "" : before.slice(brace + 1);
    const doubled = brace > 0 && before[brace - 1] === "{";
    const shaped = query !== "" && LEGACY_QUERY.test(query);
    // Where `{{ }}` also works, only a finished `{state.` asks for the old form.
    const wanted = legacyOnly ? shaped : shaped && query.startsWith("state.");
    if (brace >= 0 && !doubled && wanted) {
      // Past the key — `{state.result.fl` — there is nothing left to suggest:
      // state is offered by its top-level keys only.
      if (query.split(".").length > 2) return null;
      const candidates = rank(legacyCandidates(sources), query, true);
      if (!candidates.length) return null;
      const word = LEGACY_WORD_TAIL.exec(after)?.[0] ?? "";
      const rest = after.slice(word.length);
      const keepsTail = rest.startsWith("}") || rest.startsWith(".");
      return {
        from: lineStart + brace,
        to: cursor + word.length,
        query,
        candidates: keepsTail ? candidates.map((c) => ({ ...c, insert: c.insert.slice(0, -1) })) : candidates,
      };
    }
  }

  return null;
}

/** Replace the token with the candidate; the cursor lands after it. */
export function applyTemplate(
  text: string,
  match: Pick<TemplateMatch, "from" | "to">,
  candidate: Pick<TemplateCandidate, "insert">,
): { text: string; cursor: number } {
  return {
    text: text.slice(0, match.from) + candidate.insert + text.slice(match.to),
    cursor: match.from + candidate.insert.length,
  };
}

/**
 * Argument names from a JSON Schema: its top-level properties, and one level
 * into any that are objects — `body` and `body.data` — because `{{ body.data }}`
 * is what the request-body hint teaches.
 */
export function schemaArgs(schema: unknown): string[] {
  const propertiesOf = (node: unknown): Record<string, unknown> | null => {
    if (!node || typeof node !== "object") return null;
    const properties = (node as { properties?: unknown }).properties;
    return properties && typeof properties === "object" && !Array.isArray(properties)
      ? (properties as Record<string, unknown>)
      : null;
  };
  const top = propertiesOf(schema);
  if (!top) return [];
  const out: string[] = [];
  for (const [name, child] of Object.entries(top)) {
    out.push(name);
    for (const inner of Object.keys(propertiesOf(child) ?? {})) out.push(`${name}.${inner}`);
  }
  return out;
}

/**
 * Names and secret flags, in key order, invalid keys and duplicates dropped.
 * Whatever else a loader returned — a value, above all — does not survive this.
 */
export function normaliseVariables(input: readonly { key: string; secret?: boolean }[]): TemplateVariable[] {
  const seen = new Set<string>();
  const out: TemplateVariable[] = [];
  for (const item of input) {
    if (typeof item?.key !== "string" || !SEGMENT.test(item.key) || seen.has(item.key)) continue;
    seen.add(item.key);
    out.push({ key: item.key, secret: item.secret === true });
  }
  return out.sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * The `@query` being typed at `cursor` — only at the start of a line or after
 * whitespace, so an email address opens nothing.
 */
export function matchMention(
  text: string,
  cursor: number,
  trigger = "@",
): { from: number; to: number; query: string } | null {
  const before = text.slice(0, cursor);
  const index = before.lastIndexOf(trigger);
  if (index < 0) return null;
  const preceding = before[index - 1];
  if (preceding !== undefined && !/\s/.test(preceding)) return null;
  const query = before.slice(index + trigger.length);
  if (/[\s[\]]/.test(query)) return null;
  return { from: index, to: cursor, query };
}

/** `JSON.stringify(JSON.parse(text), null, 2)`, or why it cannot be. */
export function formatJson(text: string): { ok: true; text: string } | { ok: false; error: string } {
  try {
    return { ok: true, text: `${JSON.stringify(JSON.parse(text), null, 2)}\n` };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Not valid JSON." };
  }
}

/**
 * Why a request-body template will not be sent as JSON, or `null` when it will.
 *
 * The engine fills each reference with a JSON value and swallows the quotes
 * around it (`templating._JSON_REF`), then sends the result as JSON if it parses
 * and as plain text if it does not. So `{"ref": "{{ id }}"}` is JSON, and
 * `{"auth": "Bearer {{ id }}"}` — a reference *inside* a string — is not.
 * Replacing each reference the same way and parsing gives the same verdict.
 */
export function templateJsonProblem(text: string): string | null {
  const body = text.trim();
  if (!body) return null;
  if (/^\{\{[ \t]*[A-Za-z_][\w.-]*[ \t]*\}\}$/.test(body)) return null;
  const filled = body.replace(/"?\{\{\s*[A-Za-z_][\w.-]*\s*\}\}"?/g, "0");
  try {
    JSON.parse(filled);
    return null;
  } catch {
    return "Not valid JSON once the references are filled in, so it is sent as plain text.";
  }
}
