/**
 * Why a step failed, in the step's own words (F195).
 *
 * The engine's `step_error` carries a sanitised sentence in `error`, for
 * whatever forwards the stream unread, and the history entry's real words in
 * `reason`. A run whose journal was written before `reason` existed still has
 * the words in its record, so a reopened run reads them from the history.
 * Cloud's `lib/runErrors.ts` (F151) takes the same words from the history.
 */

type HistoryEntry = { step_id?: unknown; error?: unknown };

/** Each failed step's error, by step id, from a run's `step_history`. */
export function stepErrorsFrom(history: unknown): Record<string, string> {
    const out: Record<string, string> = {};
    if (!Array.isArray(history)) return out;
    for (const entry of history as HistoryEntry[]) {
        if (entry && typeof entry.step_id === 'string' && entry.error) {
            out[entry.step_id] = String(entry.error);
        }
    }
    return out;
}

/** A `step_error` event's reason: its own, else the record's, else its sentence. */
export function failureReason(
    event: { orch_step_id?: unknown; reason?: unknown; error?: unknown },
    fromHistory: Record<string, string>,
): string {
    if (typeof event.reason === 'string' && event.reason) return event.reason;
    const id = typeof event.orch_step_id === 'string' ? event.orch_step_id : '';
    return fromHistory[id] || String(event.error ?? '');
}

/**
 * The one line of a reason worth showing on a step card: a traceback's last
 * line names the exception; anything else leads with its point. The card
 * keeps the whole text for a hover, and the run log shows all of it.
 */
export function reasonHeadline(reason: string): string {
    const lines = reason.split('\n').map((l) => l.trim()).filter(Boolean);
    if (!lines.length) return '';
    return reason.includes('Traceback (most recent call last)') ? lines[lines.length - 1] : lines[0];
}
