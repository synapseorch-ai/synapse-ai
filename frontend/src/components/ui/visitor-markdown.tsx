/**
 * An agent's reply as a stranger on a customer's website reads it (F1518).
 *
 * The website widget drew a reply as plain text: an agent's `**The Salt Lantern**` showed its asterisks, a list
 * its dashes, and a payment link could not be clicked (VID4's bookstore takes, 11-10). This draws the same words
 * as markdown, on a policy that holds against a reply a visitor's own message may have steered, since it is drawn
 * on someone else's site:
 *
 * - **No raw HTML.** `skipHtml`: an HTML node is dropped, never sanitised and kept, so `<a href=…>x</a>` leaves
 *   `x` as text and `<img onerror>` leaves nothing.
 * - **A short list of elements**, the rest unwrapped to their text: paragraphs and breaks, strong, emphasis,
 *   strikethrough, lists, code, links, tables; a heading is a bold paragraph. No image (no tracking pixel, no
 *   button drawn as one), no rule, no task box.
 * - **Links only to http, https and mailto**, each opened in a new tab with `noopener noreferrer nofollow`. Any
 *   other scheme (`javascript:`, `data:`, `vbscript:`, a path of the widget's own app) is its text. So is a link
 *   whose words are an address other than the one it goes to: `[https://good.example](https://evil.example)`.
 * - Bare addresses are links as GFM finds them (http(s), `www.`, an email), never past the punctuation that ends
 *   a sentence. A single line break stays one, as the plain bubble kept it.
 *
 * Its look is the host's: no classes of its own beyond the elements, so the widget's stylesheet (its accent, its
 * font, light or dark) draws it. Kept apart from `Markdown`, the app's own, which links the app's paths in place
 * and carries the app's tokens; this one is synced to both products' widgets with the same rules.
 */

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { keepLineBreaks, type MdNode } from "./markdown";

/**
 * The elements a reply may draw; anything else is unwrapped to its children (an image or a rule to nothing). A
 * table is drawn (Naveen 11-10: unwrapped, VID4 take 7's catalogue table read as one run-on line): elements only,
 * the one attribute on a cell GFM's own alignment (a text-align of left, center or right), never the model's.
 */
const ALLOWED = ["p", "br", "strong", "em", "del", "ul", "ol", "li", "code", "pre", "a",
                 "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td"];

/** The schemes a reply's link may open: the web and mail. */
const SAFE_HREF = /^(?:https?:|mailto:)/i;

/** Words that read as a web address. */
const LOOKS_LIKE_URL = /^(?:https?:\/\/|www\.)\S+$/i;

/** An href kept only when it opens the web or mail; '' otherwise, which `a` then draws as text. */
export function visitorHref(url: string): string {
  return SAFE_HREF.test(url.trim()) ? url.trim() : "";
}

function bare(address: string): string {
  return address.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/+$/, "");
}

/** Whether a link's words name an address other than the one it opens (a link dressed as another). */
export function disguised(words: string, href: string): boolean {
  return LOOKS_LIKE_URL.test(words.trim()) && bare(words) !== bare(href);
}

function textOf(node: MdNode | undefined): string {
  if (!node) return "";
  if (typeof node.value === "string") return node.value;
  return (node.children ?? []).map(textOf).join("");
}

/** A cell's alignment, only when it is GFM's own: left, center or right; any other style is dropped. */
export function aligned(style: unknown): { textAlign: "left" | "center" | "right" } | undefined {
  const value = (style as { textAlign?: unknown } | undefined)?.textAlign;
  return value === "left" || value === "center" || value === "right" ? { textAlign: value } : undefined;
}

const strongHeading: Components["h1"] = ({ children }) => <p><strong>{children}</strong></p>;

const COMPONENTS: Components = {
  a: ({ href, children, node }) => {
    const words = textOf(node as unknown as MdNode);
    if (!href || disguised(words, href)) return <>{children}</>;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
    );
  },
  h1: strongHeading, h2: strongHeading, h3: strongHeading, h4: strongHeading, h5: strongHeading, h6: strongHeading,
  // A wide table scrolls sideways inside its bubble rather than widening it (the host styles `[data-table]`).
  table: ({ children }) => (
    <div data-table="">
      <table>{children}</table>
    </div>
  ),
  // A cell keeps GFM's alignment and nothing else, checked here rather than trusted (eb 11-10).
  th: ({ children, style }) => <th style={aligned(style)}>{children}</th>,
  td: ({ children, style }) => <td style={aligned(style)}>{children}</td>,
};

const PLUGINS = [remarkGfm, keepLineBreaks];

export function VisitorMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={PLUGINS}
      skipHtml
      allowedElements={ALLOWED}
      unwrapDisallowed
      urlTransform={visitorHref}
      components={COMPONENTS}
    >
      {children}
    </ReactMarkdown>
  );
}
