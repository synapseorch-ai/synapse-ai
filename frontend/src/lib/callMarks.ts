/**
 * What a chat turn's tool chip says of each call: one that was not run, and why (U68). Kept out of the page so
 * every turn, live or reopened, reads its calls the same way.
 */

/** A call as the chip reads it: a live one from its events, a reopened one as its turn saved it (F242). */
export type MarkedCall = { tool: string; result?: string; refused?: boolean; failed?: boolean; reason?: string };

/** A `tool_result` event's part of its call: what it returned, and whether it was refused, with why. */
export function resultOf(data: Record<string, unknown>): Partial<MarkedCall> {
  return {
    result: String(data.preview ?? ''),
    ...(data.refused ? { refused: true, reason: String(data.reason ?? '') } : {}),
  };
}

/** The word beside a call in the chip, or null for one that ran. */
export function callNote(call: MarkedCall): string | null {
  if (call.refused) return `not run${call.reason ? ` — ${call.reason}` : ''}`;
  return null;
}

/** How many of a turn's calls the chip's label names: those not run. */
export function tallyOf(calls: MarkedCall[]): { notRun: number; failed?: number } {
  return { notRun: calls.filter(c => c.refused).length };
}
