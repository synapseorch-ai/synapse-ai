/**
 * The markdown editor's toolbar, as text in and an edit out.
 *
 * Pure, so every button is testable without a DOM, and so the editor can apply
 * an edit through the browser's own insert path and keep undo working. An edit
 * replaces `[from, to)` with `insert`; the selection afterwards is in the
 * coordinates of the new text.
 *
 * Every action toggles, the way GitHub's do: bold on bold text removes it, a
 * list over lines that are already that list takes the markers off.
 */

export type MarkdownEdit = {
  from: number;
  to: number;
  insert: string;
  selectionStart: number;
  selectionEnd: number;
};

export type LinePrefix = "heading" | "bullet" | "numbered" | "quote";

/** `**bold**`, `_italic_`, `` `code` `` — or a fenced block when code spans lines. */
export function toggleWrap(text: string, start: number, end: number, marker: string, placeholder: string): MarkdownEdit {
  const m = marker.length;
  const selected = text.slice(start, end);

  // The markers sit just outside the selection: unwrap them.
  if (start >= m && text.slice(start - m, start) === marker && text.slice(end, end + m) === marker) {
    return { from: start - m, to: end + m, insert: selected, selectionStart: start - m, selectionEnd: end - m };
  }
  // The selection includes its own markers: unwrap those.
  if (selected.length >= 2 * m && selected.startsWith(marker) && selected.endsWith(marker)) {
    const inner = selected.slice(m, selected.length - m);
    return { from: start, to: end, insert: inner, selectionStart: start, selectionEnd: start + inner.length };
  }
  if (marker === "`" && selected.includes("\n")) {
    const lead = start > 0 && text[start - 1] !== "\n" ? "\n" : "";
    const insert = `${lead}\`\`\`\n${selected}\n\`\`\``;
    const bodyStart = start + lead.length + 4;
    return { from: start, to: end, insert, selectionStart: bodyStart, selectionEnd: bodyStart + selected.length };
  }
  const body = selected || placeholder;
  return {
    from: start,
    to: end,
    insert: `${marker}${body}${marker}`,
    selectionStart: start + m,
    selectionEnd: start + m + body.length,
  };
}

const PREFIX: Record<LinePrefix, { has: RegExp; add: (index: number) => string }> = {
  heading: { has: /^#{1,6} /, add: () => "### " },
  bullet: { has: /^[-*+] /, add: () => "- " },
  numbered: { has: /^\d+\. /, add: (index) => `${index + 1}. ` },
  quote: { has: /^> ?/, add: () => "> " },
};

/** Put a marker at the start of every line the selection touches, or take it off if they all have one. */
export function toggleLinePrefix(text: string, start: number, end: number, kind: LinePrefix): MarkdownEdit {
  const lineStart = text.lastIndexOf("\n", start - 1) + 1;
  // A selection ending just after a newline does not reach into the next line.
  const reach = end > start && text[end - 1] === "\n" ? end - 1 : end;
  const nextBreak = text.indexOf("\n", reach);
  const lineEnd = nextBreak < 0 ? text.length : nextBreak;
  const lines = text.slice(lineStart, lineEnd).split("\n");
  const { has, add } = PREFIX[kind];

  const written = lines.filter((line) => line.trim() !== "");
  const removing = written.length > 0 && written.every((line) => has.test(line));
  // Switching between the two list kinds replaces the marker rather than stacking `1. - one`.
  const existing = kind === "bullet" || kind === "numbered" ? /^([-*+]|\d+\.) / : has;
  let counter = 0;
  const next = lines.map((line) => {
    if (removing) return line.replace(has, "");
    if (line.trim() === "" && lines.length > 1) return line;
    return add(counter++) + line.replace(existing, "");
  });
  const insert = next.join("\n");

  if (start === end && lines.length === 1) {
    const shift = insert.length - (lineEnd - lineStart);
    const cursor = Math.max(lineStart, start + shift);
    return { from: lineStart, to: lineEnd, insert, selectionStart: cursor, selectionEnd: cursor };
  }
  return { from: lineStart, to: lineEnd, insert, selectionStart: lineStart, selectionEnd: lineStart + insert.length };
}

/** `[text](url)`, with the part still to be written selected. */
export function insertLink(text: string, start: number, end: number): MarkdownEdit {
  const selected = text.slice(start, end);
  if (/^https?:\/\/\S+$/.test(selected)) {
    // A URL was selected: it is the target, and the words are still to come.
    return { from: start, to: end, insert: `[](${selected})`, selectionStart: start + 1, selectionEnd: start + 1 };
  }
  const label = selected || "text";
  const insert = `[${label}](url)`;
  const urlStart = start + label.length + 3;
  return selected
    ? { from: start, to: end, insert, selectionStart: urlStart, selectionEnd: urlStart + 3 }
    : { from: start, to: end, insert, selectionStart: start + 1, selectionEnd: start + 1 + label.length };
}
