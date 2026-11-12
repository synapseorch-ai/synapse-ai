/**
 * The name an Add button gives a new item of a mapping by name (F1559): an Evaluator's routes (`route_map`) and a
 * Switch's cases (`switch_cases`).
 *
 * Add route and Add case named the new one after the count, `route_<n + 1>`; after a delete that is a name the step
 * still has, and a mapping keeps one value per name, so the add replaced that route's target — the route went to End.
 * F1557's Add field did the same to a Human step's fields (`humanFields.nextHumanFieldName`). Import-free, so a test
 * runs it in node.
 */

/** `<prefix><n>` from the count up, past every name `taken` holds: never one the step has. */
export function nextFreeName(prefix: string, taken: readonly string[]): string {
    const names = new Set(taken);
    let n = taken.length + 1;
    while (names.has(`${prefix}${n}`)) n += 1;
    return `${prefix}${n}`;
}
