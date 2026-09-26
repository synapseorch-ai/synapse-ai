/**
 * Small wording rules shared by screens, so each is decided once.
 *
 * The OSS test pass (2026-09-25) found them written inline and gone wrong in
 * places: "1 criteria", "over 1 runs", "Run all 1 case?" (F205). Cloud keeps the
 * same helper in its own `lib/copy.ts`.
 */

/** `1 criterion`, `3 criteria` — for words whose plural is not just an `s`. */
export function count(n: number, singular: string, plural = `${singular}s`): string {
    return `${n} ${n === 1 ? singular : plural}`;
}
