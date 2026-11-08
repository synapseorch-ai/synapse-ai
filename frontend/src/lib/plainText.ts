/**
 * Markdown read as plain text, for the places that cannot draw it: the bell's
 * rows and the OS notification. The bell showed a question's `**vault_write**`
 * exactly as the step's prompt spelled it.
 *
 * Not a parser. The marks a question actually carries come off (emphasis, code
 * ticks and fences, headings, list and quote markers, links and images to their
 * words), lines join into one, and anything else is left as written, so a
 * snake_case name or a stray asterisk survives.
 */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[^\n]*\n?/g, '')                          // fence lines, keeping what they hold
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')              // images → their alt text
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')               // links → their words
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')                    // headings
    .replace(/^\s{0,3}>\s?/gm, '')                         // quotes
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, '')             // list markers
    .replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, '$2')       // bold
    .replace(/(^|[^\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\w)/g, '$1$2')  // *italic*
    .replace(/(^|[^\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)/g, '$1$2')    // _italic_, not snake_case
    .replace(/~~(.+?)~~/g, '$1')                           // strikethrough
    .replace(/`([^`\n]+)`/g, '$1')                         // inline code
    .replace(/\s*\n\s*/g, ' ')                             // one line
    .trim();
}
