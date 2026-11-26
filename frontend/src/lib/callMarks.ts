/**
 * What a screen says of an agent's call: one that was not run, and why (U68), and one that ran and whose tool said
 * it failed, and why (F1751: an agent's call that failed is marked `failed` on its event and its saved call). Kept out
 * of the screens so every one of them (the chat, a helper's or a member's lane, the test panel, a phone call's log,
 * a run's log, the builder's trail) reads its calls the same way, live or reopened (F1752).
 */

/** A call as a screen reads it: a live one from its events, a reopened one as its turn saved it (F242). */
export type MarkedCall = { tool: string; result?: string | null; refused?: boolean; failed?: boolean; reason?: string | null };

/**
 * How the engine's failures read before calls carried a mark: kept only for a call with no mark at all (a record
 * saved before F1751/F1752). A marked call is what its mark says, whatever its words start with.
 */
const UNMARKED_FAILURE = /^(error|blocked|tool removed|tool not found)/i;

/** A `tool_result` event's part of its call: what it returned, and whether it was refused or failed, with why. */
export function resultOf(data: Record<string, unknown>):
    { result: string; refused?: boolean; failed?: boolean; reason?: string } {
  return {
    result: String(data.preview ?? ''),
    ...(data.refused ? { refused: true, reason: String(data.reason ?? '') }
      : data.failed ? { failed: true, reason: String(data.reason ?? '') } : {}),
  };
}

/** Whether a call failed: its mark; a call with no mark, by how the engine's failures used to read. */
export function isFailed(call: MarkedCall): boolean {
  if (call.refused) return false;
  if (call.failed !== undefined) return call.failed === true;
  return UNMARKED_FAILURE.test((call.result ?? '').trim());
}

/** The word beside a call, or null for one that ran. */
export function callNote(call: MarkedCall): string | null {
  if (call.refused) return `not run${call.reason ? ` — ${call.reason}` : ''}`;
  if (isFailed(call)) return `failed${call.reason ? ` — ${call.reason}` : ''}`;
  return null;
}

/** How many of a turn's calls the chip's label names: those not run, and those that failed. */
export function tallyOf(calls: MarkedCall[]): { notRun: number; failed: number } {
  return { notRun: calls.filter(c => c.refused).length, failed: calls.filter(isFailed).length };
}

/**
 * What a screen shows under a call as its result: what a call that ran returned. Nothing for a call marked failed: its
 * result is the observation its agent was told ("…tell the person it failed"), an instruction to the agent, and the
 * note beside it already says why (F1751; the engine marks every failure with its reason). A failure with no reason
 * (an older record, read by its words) keeps its words: they are its only reason.
 */
export function shownResult(call: MarkedCall): string {
  return call.failed === true && !call.refused && call.reason ? '' : (call.result ?? '');
}

/**
 * A one-line trail's words for a call's result (a run's log, the builder's trail), and whether they are a failure's
 * or a call not run: its note when it says why, else what it returned (an older failure's own words).
 */
export function resultLine(call: MarkedCall): { text: string; marked: boolean } {
  const note = callNote(call);
  const shown = shownResult(call);
  return { text: note && (call.reason || !shown) ? note : shown, marked: note !== null };
}
