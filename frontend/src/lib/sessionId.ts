/**
 * A new conversation's id: a random UUID (F1471).
 *
 * `crypto.randomUUID` exists only in a secure context (https, or localhost). Synapse opened over plain http on a LAN
 * address has no `randomUUID`, so the id is made from `crypto.getRandomValues`, which every browser has there too.
 * Never `Math.random` or the clock (CodeQL js/insecure-randomness, PR #379): an id is not what lets anyone into a
 * conversation (every route asks for the access token, a sign-in or an API key), but a guessable one is still not
 * how an id should be made.
 */
export function newSessionId(c: Pick<Crypto, 'getRandomValues'> & Partial<Pick<Crypto, 'randomUUID'>> = globalThis.crypto): string {
    if (typeof c.randomUUID === 'function') return c.randomUUID();
    const b = c.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; // version 4
    b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
    const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
