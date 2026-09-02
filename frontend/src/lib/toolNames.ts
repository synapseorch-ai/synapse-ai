/**
 * Human names for tool dispatch keys.
 *
 * A tool's key is a routing fact — `{server}__{tool}`, stored in steps and
 * printed in run logs — but it is not a label. Screens that ask a person to
 * *choose* a tool show this instead: the part after the server prefix, with
 * separators as spaces and words capitalized. `google-workspace__gmail_send`
 * reads "Gmail Send"; the key stays available (small, mono, or a title
 * attribute) wherever the person may need to match it against a log.
 */
export function humanizeToolName(name: string): string {
    const split = name.lastIndexOf('__');
    const tail = split === -1 ? name : name.slice(split + 2);
    return tail
        .split(/[-_]+/)
        .filter(Boolean)
        .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
        .join(' ');
}
