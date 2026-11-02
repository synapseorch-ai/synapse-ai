/**
 * Markdown, read as prose.
 *
 * There were two of these — one in `lib/utils.tsx` behind `renderTextContent`,
 * one as `RICH_MD_COMPONENTS` inside the orchestration tab — for the same job:
 * render an assistant's reply. They had drifted apart on spacing, on heading
 * weight, on whether a link gets an icon, and both were written against raw
 * `zinc-*`. That palette does work in light theme, but only because
 * `globals.css` inverts ~2.5k Tailwind colour classes underneath it; a renderer
 * that leans on the compatibility layer is a renderer that breaks the first
 * time a shade nobody inverted shows up. (One already had: `blue-300`, on link
 * hover, is not in the inversion list.)
 *
 * So: one map, on the semantic tokens. It is in the kit rather than beside the
 * chat screen because both products render assistant markdown and there is no
 * product knowledge in here at all.
 *
 * No `variant` prop. A dimmer reading — previewing a system prompt inside a
 * form, say — is `className="text-text-muted"` on the wrapper, because every
 * child that does not set its own colour inherits. One className beats an enum
 * that has to grow a case each time a surface wants something slightly else.
 *
 * `breaks` keeps a single line break as a line break. CommonMark reads it as a
 * space, which is right for a model's markdown and wrong for a question a
 * person wrote line by line: "Who asked: the travel desk\nDue: Friday" ran
 * together into one line on the run page's answer card (U70).
 */

import { ExternalLink } from "lucide-react";
import NextLink from "next/link";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/cn";

/**
 * One of this app's own paths: a single "/" then anything but a slash. A "can't" ends with its screen's link,
 * `[Evals](/evals)` (F1265), which opens in place; `//host` (protocol-relative) and `/\host` are other sites and
 * stay outside links, as does every scheme. The test is on the href, since a model's words come through here too.
 */
const IN_APP = /^\/(?![\/\\])/;

/** Whether `href` is one of this app's own paths (see `IN_APP`); cloud's vitest tries it. */
export function inAppHref(href: string | undefined): href is string {
  return !!href && IN_APP.test(href);
}

const COMPONENTS: Components = {
  // Body copy carries the rhythm; `space-y` on the wrapper cannot, because
  // markdown's blocks are siblings of wildly different heights.
  p: ({ children }) => <p className="mb-2.5 leading-relaxed last:mb-0">{children}</p>,

  h1: ({ children }) => (
    <h1 className="mb-2 mt-4 border-b border-border pb-1 text-lg font-semibold text-text first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 mt-4 text-base font-semibold text-text first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => <h3 className="mb-1 mt-3 text-sm font-semibold text-text first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-2.5 text-sm font-semibold text-text-muted first:mt-0">{children}</h4>,
  // Both previous maps stopped at h4, so a document that used h5 fell through
  // to the browser's default — bold, serif-ish, larger than the h4 above it.
  h5: ({ children }) => <h5 className="mb-1 mt-2 text-xs font-semibold uppercase tracking-wide text-text-muted first:mt-0">{children}</h5>,
  h6: ({ children }) => <h6 className="mb-1 mt-2 text-xs font-semibold text-text-faint first:mt-0">{children}</h6>,

  // `list-outside` rather than `list-inside`: inside, the second line of a
  // wrapped item tucks under the marker instead of aligning with the first.
  ul: ({ children }) => <ul className="mb-2.5 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2.5 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,

  // A link within the page — an answer's [K#] to its Sources row — scrolls to
  // its target and focuses it, without a new tab or a change to the address.
  a: ({ href, children }) => href?.startsWith('#') ? (
    <a
      href={href}
      onClick={event => {
        const target = document.getElementById(decodeURIComponent(href.slice(1)));
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({ block: 'nearest' });
        target.focus({ preventScroll: true });
      }}
      className="text-accent-ink underline underline-offset-2 hover:text-accent-hover"
    >
      {children}
    </a>
  ) : inAppHref(href) ? (
    <NextLink href={href} className="text-accent-ink underline underline-offset-2 hover:text-accent-hover">
      {children}
    </NextLink>
  ) : (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 break-all text-accent-ink underline underline-offset-2 hover:text-accent-hover"
    >
      {children}
      <ExternalLink className="inline h-3 w-3 shrink-0" aria-hidden />
    </a>
  ),

  // The block form is bare: `pre` below owns the surface, the border, the
  // padding and the scroll. Painting both is why fenced blocks had two
  // borders, one inside the other.
  //
  // `node.position` rather than `Boolean(className)`: a fence written without a
  // language — which is what models produce for shell output, JSON dumps and
  // stack traces — gets no `language-*` class, so the class test called the
  // commonest kind of block inline and rendered it as a padded pill inside
  // `pre`'s own box. Spanning more than one line is what actually makes it a
  // block. Both maps this replaces had the same bug.
  code: ({ className, children, node }) => {
    const position = node?.position;
    const isBlock = Boolean(className) ||
      (position ? position.end.line > position.start.line : false);
    return isBlock ? (
      <code className={cn("font-code text-[13px]", className)}>{children}</code>
    ) : (
      <code className="rounded-sm bg-surface-2 px-1.5 py-0.5 font-code text-[13px] text-text">{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="mb-2.5 overflow-x-auto rounded-md border border-border bg-surface-2 p-3 font-code text-[13px] text-text last:mb-0">
      {children}
    </pre>
  ),

  // Wide content scrolls inside its own box. Without this the page itself
  // scrolls sideways, which in a 620px panel is immediate.
  table: ({ children }) => (
    <div className="mb-2.5 overflow-x-auto last:mb-0">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-surface-2 text-left text-text-muted">{children}</thead>,
  th: ({ children }) => (
    <th className="border border-border px-3 py-2 text-2xs font-semibold uppercase tracking-wider">{children}</th>
  ),
  td: ({ children }) => <td className="border border-border px-3 py-2">{children}</td>,
  tr: ({ children }) => <tr className="transition-colors hover:bg-row-hover">{children}</tr>,

  // Neither previous map had an `img`, so a markdown image rendered at its
  // natural size — out through the side of a 620px panel, for anything wide.
  img: ({ src, alt }) => (
    // Not `next/image`: the src comes from whatever markdown the model wrote,
    // so there is no known host to configure a loader for and no dimensions to
    // give it.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={typeof src === "string" ? src : undefined}
      alt={alt ?? ""}
      loading="lazy"
      className="my-2 h-auto max-w-full rounded-md border border-border"
    />
  ),

  blockquote: ({ children }) => (
    <blockquote className="mb-2.5 border-l-2 border-border-strong pl-4 text-text-muted last:mb-0">{children}</blockquote>
  ),
  hr: () => <hr className="my-4 border-border" />,
  strong: ({ children }) => <strong className="font-semibold text-text">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="text-text-faint line-through">{children}</del>,
};

export type MdNode = { type: string; value?: string; children?: MdNode[] };

/**
 * What `remark-breaks` does, without the dependency: each newline inside text
 * becomes a hard break (`<br>`). Code and inline code are not `text`, so they
 * keep their own lines. Exported for its test (cloud's Vitest).
 */
export function keepLineBreaks() {
  const walk = (node: MdNode) => {
    if (!node.children) return;
    node.children = node.children.flatMap((child): MdNode[] => {
      if (child.type !== "text" || !child.value?.includes("\n")) {
        walk(child);
        return [child];
      }
      return child.value.split(/\r?\n/).flatMap((part, i): MdNode[] =>
        i === 0 ? [{ type: "text", value: part }] : [{ type: "break" }, { type: "text", value: part }],
      );
    });
  };
  return (tree: MdNode) => walk(tree);
}

const PLUGINS = [remarkGfm];
const PLUGINS_KEEPING_BREAKS = [remarkGfm, keepLineBreaks];

/** A leading `---` … `---` (or `...`) block, as a skill's or a note's metadata opens a file. */
const FRONT_MATTER = /^\uFEFF?---[ \t]*\r?\n(?:([\s\S]*?)\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;

/**
 * A file's YAML front matter and the body after it (F505). Markdown reads the block as a
 * thematic break, text and a setext heading — the closing `---` underlines the last line — so
 * the Vault's preview showed `name: x` as a heading. No block: `yaml` is null and the text is
 * unchanged. Exported for its test (cloud's Vitest).
 */
export function splitFrontMatter(text: string): { yaml: string | null; body: string } {
  const found = FRONT_MATTER.exec(text);
  return found ? { yaml: found[1] ?? "", body: text.slice(found[0].length) } : { yaml: null, body: text };
}

/** The front matter as a quiet `yaml` code block above the body: shown, never a heading. Exported for its test. */
export function frontMatterShown(text: string): string {
  const { yaml, body } = splitFrontMatter(text);
  if (yaml === null) return text;
  // A fence longer than any run of backticks inside, so the block cannot close early.
  const fence = "`".repeat(Math.max(3, ...Array.from(yaml.matchAll(/`+/g), (m) => m[0].length + 1)));
  return `${fence}yaml\n${yaml}\n${fence}\n\n${body}`;
}

export function Markdown({
  children,
  className,
  breaks = false,
  frontMatter = false,
}: {
  children: string;
  className?: string;
  /** Keep single line breaks (a person's question), rather than CommonMark's spaces. */
  breaks?: boolean;
  /** A file's leading YAML block shows as a `yaml` code block, not as a heading (the Vault's files). */
  frontMatter?: boolean;
}) {
  if (!children) return null;
  return (
    // `break-words`: a long unbroken token — a URL, a path, base64 — breaks
    // inside its line instead of running out of the message (F217). It does
    // not change a table's or a code block's own width, so both still scroll.
    <div className={cn("text-sm text-text break-words", className)}>
      <ReactMarkdown remarkPlugins={breaks ? PLUGINS_KEEPING_BREAKS : PLUGINS} components={COMPONENTS}>
        {frontMatter ? frontMatterShown(children) : children}
      </ReactMarkdown>
    </div>
  );
}
