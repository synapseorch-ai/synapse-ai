/**
 * What to tell a person whose answer to a paused run was refused (F215).
 *
 * The backend claims a paused run for exactly one answer (F136), so when two
 * people answer the same question at once, one of them gets a 409. That person
 * must hear it: their dialog has already closed on an answer that never
 * applied. Shared by the canvas and chat, the two places a run is answered.
 */
export async function refusedAnswerMessage(res: Response): Promise<string> {
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    const detail = typeof body.detail === 'string' ? body.detail : '';
    // `RunAlreadyActive`: another answer holds the run and it is running on it.
    if (res.status === 409 && /already active/i.test(detail)) {
        return 'Someone else answered this question first. The run is carrying on with their answer.';
    }
    if (res.status === 404) return 'This run no longer exists.';
    // e.g. "The run is completed and is not waiting for an answer."
    return detail || `Your answer was not accepted (HTTP ${res.status}).`;
}
