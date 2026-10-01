"""A web page's readable content as Markdown — for uploaded `.html`, email bodies, EPUB chapters,
and S1's fetched pages (`html_to_markdown`, the function S1 calls).

What is kept is what a reader of the page reads:

* **Never content:** scripts, styles, forms, embeds, and the page's own
  navigation, header, footer and sidebars. Inside the content, blocks whose
  class or id says they are chrome (cookie banners, share bars, "related"
  lists, newsletters, ads) go too.
* **Hidden from a reader, so left out and counted** (`hidden_removed`):
  elements with `hidden`, `aria-hidden="true"`, or an inline style of
  `display:none`, `visibility:hidden`, `font-size:0` or `opacity:0` — where a
  prompt hides in a page (EchoLeak's class of attack). HTML comments go too,
  counted apart.
* **The content root:** `<main>`, else the one `<article>`, else
  `role="main"`, else the body.
* **Markdown:** headings, paragraphs, lists, tables, code, quotes. Links keep
  their text only (no remote URL is rendered; 16 §3.8); images are dropped.

bs4 with the standard library's parser: no new dependency, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parsed import ParsedDocument, ParserInfo, ParseWarning

PARSER = ParserInfo(name="html", version="1")

_DROP = ("script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "canvas", "form", "button",
         "input", "select", "textarea", "label", "dialog", "img", "picture", "video", "audio", "source", "map")
_CHROME = ("nav", "aside", "header", "footer")
_CHROME_WORDS = frozenset({"cookie", "cookies", "consent", "banner", "share", "sharing", "social", "related",
                           "newsletter", "subscribe", "advert", "advertisement", "ads", "promo", "breadcrumb",
                           "breadcrumbs", "sidebar", "comments", "popup", "modal", "skip", "toc"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)"
                           r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", re.IGNORECASE)
_SPACE = re.compile(r"[ \t\r\f\v]+")
_BLOCK = frozenset({"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
                    "table", "pre", "blockquote", "dl", "dt", "dd", "figure", "figcaption", "hr", "br", "tr", "td",
                    "th", "address", "details", "summary", "center", "header", "footer", "nav", "aside", "hgroup",
                    "fieldset", "caption"})


@dataclass
class HtmlExtract:
    """`markdown` is the readable content. The rest is the page's own metadata, for a document's
    title and date, and the counts behind its warnings."""

    markdown: str
    title: str | None = None
    language: str | None = None
    published: str | None = None
    canonical_url: str | None = None
    hidden_removed: int = 0
    comments_removed: int = 0
    links: list[str] = field(default_factory=list)  # absolute (when `url` is given), in order, deduplicated


def html_to_markdown(html: str | bytes, *, url: str | None = None) -> HtmlExtract:
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(html, "html.parser")
    comments = soup.find_all(string=lambda s: isinstance(s, Comment))
    for c in comments:
        c.extract()
    meta = _metadata(soup, url)
    for tag in soup.find_all(_DROP):
        tag.decompose()
    hidden = 0
    for tag in soup.find_all(_is_hidden):
        if tag.decomposed:
            continue
        if tag.get_text(strip=True):
            hidden += 1
        tag.decompose()
    root = _content_root(soup)
    for tag in list(root.find_all(_CHROME)):
        if tag.decomposed or (tag.name == "header" and tag.find(["h1", "h2", "h3"]) is not None):
            continue  # an article's own header holds its title
        tag.decompose()
    for tag in list(root.find_all(_is_chrome_block)):
        if not tag.decomposed:
            tag.decompose()
    links = []
    for a in root.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            absolute = urljoin(url, href) if url else href
            if absolute not in links:
                links.append(absolute)
    lines = _Markdown().render(root)
    return HtmlExtract(markdown=lines, hidden_removed=hidden, comments_removed=len(comments), links=links, **meta)


def read_html(data: bytes, ctx: ReadContext) -> ParsedDocument:
    from core.knowledge.parse import decode

    raw, warnings = decode(data)
    return markdown_document(html_to_markdown(raw, url=ctx.source.url), ctx.source, PARSER, warnings)


def markdown_document(extract: HtmlExtract, source: Any, parser: ParserInfo, warnings: list[ParseWarning], *,
                      title: str | None = None) -> ParsedDocument:
    """A `ParsedDocument` whose canonical text is `extract.markdown` (as a Markdown file's is its text)."""
    from core.knowledge.parse.sanitize import sanitize_text
    from core.knowledge.parse.text import MAX_TEXT_CHARS, parse_markdown
    from core.knowledge.parsed import ParseStats

    clean, found = sanitize_text(extract.markdown)
    warnings = [*warnings, *found]
    truncated = len(clean) > MAX_TEXT_CHARS
    clean = clean[:MAX_TEXT_CHARS]
    blocks, heading = parse_markdown(clean)
    if extract.hidden_removed:
        warnings.append(ParseWarning(code="hidden_text_removed", count=extract.hidden_removed, message=(
            f"{extract.hidden_removed} hidden element(s) left out: content a reader of the page never sees.")))
    if extract.comments_removed:
        warnings.append(ParseWarning(code="html_comments_removed", count=extract.comments_removed,
                                     message=f"{extract.comments_removed} HTML comment(s) removed: a reader never "
                                             "sees them."))
    if not blocks:
        warnings.append(ParseWarning(code="empty_document", message="The page has no readable text."))
    if truncated:
        warnings.append(ParseWarning(code="truncated_size", message=(
            "The page's text was cut at the per-document limit; the rest is not indexed.")))
    stats = ParseStats(chars=len(clean), tables=sum(1 for b in blocks if b.kind == "table"), truncated=truncated,
                       truncated_reason="truncated_size" if truncated else None)
    return ParsedDocument(source=source, parser=parser, text=clean, blocks=blocks, warnings=warnings, stats=stats,
                          title=title or extract.title or heading["title"], language=extract.language,
                          doc_date=extract.published)


# ── what to drop ───────────────────────────────────────────────────────────────

def _is_hidden(tag) -> bool:
    if getattr(tag, "attrs", None) is None:
        return False
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    style = tag.get("style")
    return bool(style and _HIDDEN_STYLE.search(str(style)))


def _words(tag) -> set[str]:
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    raw = " ".join([*classes, str(tag.get("id") or "")]).lower()
    return set(re.split(r"[^a-z0-9]+", raw)) - {""}


def _is_chrome_block(tag) -> bool:
    return tag.name in ("div", "section", "ul", "ol", "aside", "p") and bool(_words(tag) & _CHROME_WORDS)


def _content_root(soup):
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None:
        return main
    articles = soup.find_all("article")
    if len(articles) == 1:
        return articles[0]
    return soup.body or soup


def _metadata(soup, url: str | None) -> dict[str, Any]:
    def meta(*names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag is not None and tag.get("content"):
                return str(tag["content"]).strip()
        return None

    title = meta("og:title")
    if title is None and soup.title is not None and soup.title.string:
        title = soup.title.string.strip()
    html = soup.find("html")
    language = str(html.get("lang")).strip() if html is not None and html.get("lang") else None
    published = meta("article:published_time", "datePublished", "date", "dc.date", "DC.date.issued")
    if published is None:
        time_tag = soup.find("time", attrs={"datetime": True})
        published = str(time_tag["datetime"]) if time_tag is not None else None
    canonical = soup.find("link", rel="canonical")
    href = str(canonical["href"]).strip() if canonical is not None and canonical.get("href") else None
    return {"title": title or None, "language": language,
            "published": published[:10] if published and re.match(r"^\d{4}-\d{2}-\d{2}", published) else None,
            "canonical_url": urljoin(url, href) if href and url else href}


# ── to Markdown ────────────────────────────────────────────────────────────────

class _Markdown:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def render(self, root) -> str:
        self._children(root)
        return "\n\n".join(b for b in self.blocks if b.strip())

    def _inline(self, node) -> str:
        from bs4 import NavigableString

        if isinstance(node, NavigableString):
            return str(node)
        if node.name == "br":
            return "\n"
        if node.name in ("code", "kbd", "samp") and node.parent is not None and node.parent.name != "pre":
            text = node.get_text()
            return f"`{text}`" if text.strip() else text
        return "".join(self._inline(c) for c in node.children)

    def _text(self, node) -> str:
        text = self._inline(node)
        return "\n".join(_SPACE.sub(" ", line).strip() for line in text.split("\n")).strip()

    def _children(self, node) -> None:
        from bs4 import NavigableString

        run: list[str] = []

        def flush() -> None:
            text = "\n".join(_SPACE.sub(" ", line).strip() for line in "".join(run).split("\n")).strip()
            if text:
                self.blocks.append(text)
            run.clear()

        for child in node.children:
            if isinstance(child, NavigableString):
                run.append(str(child))
            elif child.name in _BLOCK:
                flush()
                self._block(child)
            else:
                run.append(self._inline(child))
        flush()

    def _block(self, node) -> None:
        name = node.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self._text(node).replace("\n", " ")
            if text:
                self.blocks.append("#" * int(name[1]) + " " + text)
        elif name in ("ul", "ol"):
            items = []
            for k, li in enumerate(node.find_all("li", recursive=False), start=1):
                text = self._text(li).replace("\n", " ")
                if text:
                    items.append(f"{k}. {text}" if name == "ol" else f"- {text}")
            if items:
                self.blocks.append("\n".join(items))
        elif name == "pre":
            code = node.get_text().strip("\n")
            if code.strip():
                self.blocks.append(f"```\n{code}\n```")
        elif name == "blockquote":
            inner = _Markdown()
            inner._children(node)
            quoted = "\n\n".join(inner.blocks)
            if quoted.strip():
                self.blocks.append("\n".join("> " + line if line else ">" for line in quoted.split("\n")))
        elif name == "table":
            self._table(node)
        elif name == "dl":
            for dt in node.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                term, value = self._text(dt), self._text(dd) if dd is not None else ""
                if term or value:
                    self.blocks.append(f"{term}: {value}" if value else term)
        elif name in ("hr", "br"):
            return
        else:  # p, div, section, article, li, figure, …: their own children decide
            self._children(node)

    def _table(self, node) -> None:
        rows = []
        for tr in node.find_all("tr"):
            cells = [self._text(c).replace("\n", " ").replace("|", "\\|") for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        self.blocks.append("\n".join(lines))
