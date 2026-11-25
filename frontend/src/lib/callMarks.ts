/**
 * What a chat turn's tool chip says of each call: one that was not run, and why (U68), and one that ran and whose tool
 * said it failed, and why (F1751: an agent's call that failed is marked `failed` on its event and its saved call). Kept
 * out of the page so every turn, live or reopened, reads its calls the same way.
 */

/** A call as the chip reads it: a live one from its events, a reopened one as its turn saved it (F242). */
export type MarkedCall = { tool: string; result?: string; refused?: boolean; failed?: boolean; reason?: string };

/** A `tool_result` event's part of its call: what it returned, and whether it was refused or failed, with why. */
export function resultOf(data: Record<string, unknown>): Partial<MarkedCall> {
  return {
    result: String(data.preview ?? ''),
    ...(data.refused ? { refused: true, reason: String(data.reason ?? '') }
      : data.failed ? { failed: true, reason: String(data.reason ?? '') } : {}),
  };
}

/** The word beside a call in the chip, or null for one that ran. */
export function callNote(call: MarkedCall): string | null {
  if (call.refused) return `not run${call.reason ? ` — ${call.reason}` : ''}`;
  if (call.failed) return `failed${call.reason ? ` — ${call.reason}` : ''}`;
  return null;
}

/** How many of a turn's calls the chip's label names: those not run, and those that failed. */
export function tallyOf(calls: MarkedCall[]): { notRun: number; failed: number } {
  return { notRun: calls.filter(c => c.refused).length, failed: calls.filter(c => c.failed && !c.refused).length };
}

/**
 * What the chip shows under a call as its result: what a call that ran returned. Nothing for a failed call: its
 * result is the observation its agent was told ("…tell the person it failed"), an instruction to the agent, and the
 * note beside it already says why (F1751).
 */
export function shownResult(call: MarkedCall): string {
  return call.failed && !call.refused ? '' : (call.result ?? '');
}
