"""
Platform-native Markdown formatters.
The agent produces standard Markdown. Before sending, call the appropriate
platform formatter so text renders correctly in each messaging app.
"""
import re


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _chunk(text: str, limit: int) -> list[str]:
    """Split long text into chunks of at most `limit` chars, respecting lines."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# --------------------------------------------------------------------------- #
# Telegram — MarkdownV2
# --------------------------------------------------------------------------- #
# Characters that MUST be escaped outside formatting spans
_TG_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!\\"

_TG_ESCAPE_RE = re.compile(r"([" + re.escape(_TG_ESCAPE_CHARS) + r"])")


def _tg_escape(text: str) -> str:
    return _TG_ESCAPE_RE.sub(r"\\\1", text)


def to_telegram(text: str) -> list[str]:
    """
    Convert Markdown to Telegram MarkdownV2.
    Returns a list of chunks (max 4096 chars each).
    """
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.*?)\*\*", lambda m: f"*{_tg_escape(m.group(1))}*", text, flags=re.DOTALL)
    # Italic: *text* or _text_ → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", lambda m: f"_{_tg_escape(m.group(1))}_", text)
    text = re.sub(r"_(.*?)_", lambda m: f"_{_tg_escape(m.group(1))}_", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", lambda m: f"`{m.group(1)}`", text)
    # Code blocks
    text = re.sub(r"```(\w*)\n?(.*?)```", lambda m: f"```{m.group(1)}\n{m.group(2)}```", text, flags=re.DOTALL)
    # Escape remaining characters outside formatting spans
    # Simple approach: escape chars that are not part of formatting we added
    def escape_plain(m):
        return _tg_escape(m.group(0))
    # Escape plain text segments (not inside backticks or stars)
    text = re.sub(r"(?<!\\)([" + re.escape(r"[]()~>#+-=|{}.!") + r"])", r"\\\1", text)

    return _chunk(text, 4096)


# --------------------------------------------------------------------------- #
# Discord — CommonMark (rendered natively)
# --------------------------------------------------------------------------- #

def to_discord(text: str) -> list[str]:
    """
    Discord renders standard Markdown natively.
    Main conversion: headings → bold lines, strip HTML.
    Returns chunks of max 2000 chars.
    """
    # h1-h3 → bold
    text = re.sub(r"^#{1,3}\s+(.+)$", r"**\1**", text, flags=re.MULTILINE)
    # h4-h6 → italic bold
    text = re.sub(r"^#{4,6}\s+(.+)$", r"***\1***", text, flags=re.MULTILINE)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    return _chunk(text, 2000)


# --------------------------------------------------------------------------- #
# Slack — mrkdwn
# --------------------------------------------------------------------------- #

def to_slack(text: str) -> list[str]:
    """
    Convert Markdown to Slack mrkdwn format.
    Returns chunks of max 3000 chars (Slack block text limit).
    """
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text, flags=re.DOTALL)
    # Italic: *text* or _text_ → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"_\1_", text)
    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)
    # Code blocks: ```lang\ncode``` → ```code```
    text = re.sub(r"```\w*\n?(.*?)```", r"```\1```", text, flags=re.DOTALL)
    # Headings → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Blockquotes
    text = re.sub(r"^>\s*(.+)$", r"> \1", text, flags=re.MULTILINE)
    return _chunk(text, 3000)


# --------------------------------------------------------------------------- #
# Teams — Bot Framework Markdown (subset of CommonMark)
# --------------------------------------------------------------------------- #

def to_teams(text: str) -> list[str]:
    """
    Teams supports a subset of Markdown in Bot Framework messages.
    Strip unsupported elements and return chunks.
    """
    # Strip HTML
    text = re.sub(r"<[^>]+>", "", text)
    # Horizontal rules → blank lines
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    return _chunk(text, 28_000)  # Teams card text limit


# --------------------------------------------------------------------------- #
# WhatsApp — limited formatting
# --------------------------------------------------------------------------- #

def to_whatsapp(text: str) -> list[str]:
    """
    WhatsApp supports *bold*, _italic_, ~strikethrough~, `monospace`.
    Strip everything else and map Markdown to WhatsApp format.
    Returns chunks of max 4096 chars.
    """
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text, flags=re.DOTALL)
    # Italic: _text_ stays; *text* → _text_ (if not already bold)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"_\1_", text)
    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)
    # Headings → UPPERCASE plain text
    text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: m.group(1).upper(), text, flags=re.MULTILINE)
    # Links: [text](url) → text (url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Code blocks → just strip fences, keep content
    text = re.sub(r"```\w*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    # Strip HTML
    text = re.sub(r"<[^>]+>", "", text)
    return _chunk(text, 4096)


# --------------------------------------------------------------------------- #
# Voice — plain words for text-to-speech (VOX1a, V15)
# --------------------------------------------------------------------------- #
# The voice prompt asks for no markdown; this is the second line of defence, so
# a stray "**" or a bullet is not read aloud. It runs on each phrase as it is
# streamed, so every rule works on a fragment as well as on a whole answer.

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0001F1E6-\U0001F1FF]")


def to_speech(text: str) -> str:
    """Markdown as words a voice can say: no symbols, links as their text, code dropped."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.DOTALL)        # code blocks are not read out
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)             # images → their alt text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)               # links → their text
    text = re.sub(r"https?://\S+", "a link", text)
    # A web address without its scheme, as models write them on a call ("shop.example/checkout/8de333c5"):
    # a dotted host followed by a path. A bare name ("example.com") is left: it is said as words.
    text = re.sub(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}/\S*", "a link", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)    # headings
    text = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)  # list markers
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)             # quotes
    text = re.sub(r"\*{1,3}|`+|~~|(?<!\w)_{1,2}|_{1,2}(?!\w)|\|", "", text)
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"[ \t]*\n+[ \t]*", " ", text)


def to_voice(text: str) -> list[str]:
    return [to_speech(text).strip()]


# --------------------------------------------------------------------------- #
# Dispatch helper
# --------------------------------------------------------------------------- #

FORMATTERS = {
    "telegram": to_telegram,
    "discord": to_discord,
    "slack": to_slack,
    "teams": to_teams,
    "whatsapp": to_whatsapp,
    "twilio_voice": to_voice,
}


def format_for_platform(platform: str, text: str) -> list[str]:
    """Format text for the given platform. Returns list of message chunks."""
    formatter = FORMATTERS.get(platform, to_discord)  # discord as safe default
    return formatter(text)
