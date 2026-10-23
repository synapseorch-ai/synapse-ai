/**
 * What an editor's save came back as, when it was not simply saved (F871).
 *
 * The editor sends `If-Match: "<updated_at>"` — the copy it read — and the API refuses a save from a copy someone has
 * changed since (409, with when) or of a workflow since deleted (404). QA1d met both on cloud with two tabs: the
 * stale save went in over the newer one (both 200, the first tab still "Saved"), and the deleted one's 404 was a
 * four-second toast while the bar went on saying "Unsaved changes". Both hosts read the refusal here; the dialog
 * that offers the ways on is `workbench/SaveConflictDialog`.
 */

export type SaveProblem =
    | { kind: 'changed'; at: string; message: string }
    | { kind: 'deleted'; message: string };

/** The header a save sends to say what it read; none for a copy that has no stamp (a new workflow). */
export function ifMatch(read: { updated_at?: string | null } | null | undefined): Record<string, string> {
    const at = read?.updated_at;
    return at ? { 'If-Match': `"${at}"` } : {};
}

/** A refused save as a problem the editor keeps on screen, or null for any other failure (said as before). */
export function saveProblem(status: number, detail: unknown): SaveProblem | null {
    const body = detail && typeof detail === 'object' ? (detail as { message?: unknown; updated_at?: unknown }) : {};
    const message = typeof body.message === 'string' ? body.message : '';
    if (status === 409 && typeof body.updated_at === 'string') {
        return { kind: 'changed', at: body.updated_at, message };
    }
    if (status === 404) return { kind: 'deleted', message };
    return null;
}

/** What the top bar says in place of "Unsaved changes" while a problem stands. */
export function problemLabel(problem: SaveProblem | null): string | null {
    if (!problem) return null;
    return problem.kind === 'changed' ? 'Changed elsewhere' : 'Deleted elsewhere';
}

/** "at 14:03:12" today, "on 22 Oct at 14:03" otherwise; the server's own stamp as it came if it does not parse. */
export function changedWhen(at: string, now: Date = new Date()): string {
    const when = new Date(at);
    if (Number.isNaN(when.getTime())) return `at ${at}`;
    const time = when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    if (when.toDateString() === now.toDateString()) return `at ${time}`;
    return `on ${when.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} at ${time}`;
}
