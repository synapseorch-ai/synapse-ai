// ─── SSE stall watchdog ─────────────────────────────────────────────────────
// Synapse SSE endpoints send a heartbeat comment (`: ping` / `: keepalive`)
// every ~1–10s while a long LLM/orchestration step is running, so any gap
// longer than this means the connection is dead — classically after the
// machine sleeps and the TCP socket is silently dropped, where reader.read()
// would otherwise never resolve *or* reject and the UI hangs forever. Racing
// each read against this timeout lets callers abort and recover (fall back /
// retry / surface an error) instead of hanging.
//
// Set generously (3 min) so a genuinely slow LLM turn is never mistaken for a
// dead connection even if heartbeats are delayed (slow proxy, event-loop
// starvation, GC pauses). Heartbeats every ~10s mean a healthy stream resets
// this watchdog ~18× before it could fire; the timeout only bites when the
// socket is truly dead and no bytes arrive at all.
export const STREAM_STALL_TIMEOUT_MS = 180000;

export async function readWithStallTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  controller: AbortController,
  timeoutMs: number = STREAM_STALL_TIMEOUT_MS,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  const readPromise = reader.read();
  // Swallow the eventual AbortError when the timeout wins and we abort below,
  // so it never surfaces as an unhandled rejection.
  readPromise.catch(() => {});

  let timer: ReturnType<typeof setTimeout> | undefined;
  const stall = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      // Reject first so the stall error (not the abort) wins the race
      // deterministically, then abort to free the underlying connection.
      reject(new Error('Stream stalled — connection lost'));
      controller.abort();
    }, timeoutMs);
  });

  try {
    return await Promise.race([readPromise, stall]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
