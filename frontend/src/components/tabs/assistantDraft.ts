/*
 * A new Synapse AI chat with words waiting in its composer (TABS2, D327; for ONB3's starter prompts, D326).
 *
 * The link is `?assistant=new&draft=<words>` on the chat page. Chat reads it once, opens a new Synapse AI tab with
 * the words in the composer, unsent, and takes the parameters off the address so a reload does not open another.
 * Anyone can make such a link, so the words are only ever a suggestion: they are cleaned, capped, shown under a
 * "Suggested prompt" label, and nothing runs until the person presses Send.
 */

/** The `assistant` query value that asks for a new Synapse AI chat. */
export const NEW_ASSISTANT_CHAT = 'new';

/** The most a link's draft may hold; past it the words are cut. */
export const MAX_DRAFT = 2000;

/** What the composer says above words it was handed rather than typed. */
export const SUGGESTED_LABEL = 'Suggested prompt';

// Control characters other than tab and newline, and the bidirectional overrides that can make words read
// differently from what they say.
// eslint-disable-next-line no-control-regex
const UNSAFE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F‎‏‪-‮⁦-⁩]/g;

/** `text` as a composer may hold it: no control characters, Windows line ends made plain, at most `MAX_DRAFT`. */
export function cleanDraft(text: string): string {
    return text.replace(/\r\n?/g, '\n').replace(UNSAFE, '').slice(0, MAX_DRAFT).trim();
}

/** The address that opens a new Synapse AI chat on `chatPath` (OSS `/`, cloud `/org/chat`) with `draft` waiting. */
export function assistantChatHref(draft: string, chatPath = '/'): string {
    const params = new URLSearchParams({ assistant: NEW_ASSISTANT_CHAT });
    const words = cleanDraft(draft);
    if (words) params.set('draft', words);
    return `${chatPath}?${params.toString()}`;
}

/** What `search` (a `location.search`) asks for: a new Synapse AI chat and its cleaned draft, or nothing. */
export function readAssistantDraft(search: string): { draft: string } | null {
    const params = new URLSearchParams(search);
    if (params.get('assistant') !== NEW_ASSISTANT_CHAT) return null;
    return { draft: cleanDraft(params.get('draft') ?? '') };
}

/** `href` without the parameters `readAssistantDraft` reads, so a reload does not open the chat again. */
export function withoutAssistantDraft(href: string): string {
    const url = new URL(href);
    url.searchParams.delete('assistant');
    url.searchParams.delete('draft');
    return `${url.pathname}${url.search}${url.hash}`;
}
