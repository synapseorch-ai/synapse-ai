/**
 * List filtering, in a module with no React and no `"use client"`.
 *
 * Kept out of the component that renders the search box so a server component
 * can call it: Next compiles every export of a `"use client"` module into a
 * client reference, which can be rendered but never called.
 */

/** Case-insensitive "every word appears somewhere in one of these fields". */
export function matchesQuery(query: string, ...fields: (string | undefined | null)[]): boolean {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    const hay = fields.filter(Boolean).join(' ').toLowerCase();
    return q.split(/\s+/).every(word => hay.includes(word));
}
