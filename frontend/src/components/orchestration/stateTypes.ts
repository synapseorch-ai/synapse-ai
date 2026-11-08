/**
 * One vocabulary for `state_schema` types.
 *
 * An orchestration's state schema maps a key to `{type, default, description}`.
 * Until now `type` had two spellings and no owner: the AI builder was told
 * "Allowed types: str, int, float, bool, list, dict", while the dropdown below
 * offered `string, number, boolean, list, dict`. Radix renders a *blank*
 * trigger — not its placeholder — for a value matching no option, so every
 * builder-written `str` key showed an empty type box next to a `dict` key that
 * looked fine, and nothing anywhere reconciled the two lists.
 *
 * The backend's spelling wins: it is what the builder already emits and what
 * four prompt strings already teach, and `int` vs `float` is a distinction
 * `number` cannot express but the engine's coercion needs. The old spellings
 * are read as aliases, so nothing stored needs migrating.
 *
 * `backend/core/state_types.py` is this table in Python. Change both together.
 */

export const STATE_TYPES = ["str", "int", "float", "bool", "list", "dict"] as const;

export type StateType = (typeof STATE_TYPES)[number];

/** What each type is called in the editor. Nobody should have to read `str`. */
export const STATE_TYPE_LABELS: Record<StateType, string> = {
    str: 'Text',
    int: 'Whole number',
    float: 'Decimal',
    bool: 'True or false',
    list: 'List',
    dict: 'Object',
};

/**
 * Everything else that has meant one of the above. `number` widens to `float`:
 * the old dropdown's number input accepted 7.5, so reading it as an int would
 * start rejecting values the editor used to take. Widening never does.
 */
const ALIASES: Record<string, StateType> = {
    string: 'str',
    text: 'str',
    integer: 'int',
    number: 'float',
    double: 'float',
    decimal: 'float',
    boolean: 'bool',
    array: 'list',
    object: 'dict',
    json: 'dict',
    map: 'dict',
};

/**
 * The canonical spelling of `raw`, or null if it is not a type we know.
 *
 * Null rather than a default on purpose: a key whose type cannot be read is a
 * mistake someone has to see. Silently rewriting it on render would either
 * dirty the draft merely by opening it, or save a guess.
 */
export function normalizeStateType(raw: unknown): StateType | null {
    if (typeof raw !== 'string') return null;
    const key = raw.trim().toLowerCase();
    if ((STATE_TYPES as readonly string[]).includes(key)) return key as StateType;
    return ALIASES[key] ?? null;
}

/** The value a key of this type starts life with. */
export function emptyDefaultFor(type: unknown): unknown {
    switch (normalizeStateType(type)) {
        case 'int':
        case 'float': return 0;
        case 'bool': return false;
        case 'list': return [];
        case 'dict': return {};
        default: return '';
    }
}

/**
 * Is `value` already of the declared type? An unrecognised type contradicts
 * nothing, so it matches: nothing can be claimed about a type we cannot read.
 */
export function isValidDefaultFor(type: unknown, value: unknown): boolean {
    switch (normalizeStateType(type)) {
        case 'str': return typeof value === 'string';
        case 'int': return Number.isInteger(value);
        case 'float': return typeof value === 'number' && Number.isFinite(value);
        case 'bool': return typeof value === 'boolean';
        case 'list': return Array.isArray(value);
        case 'dict':
            return typeof value === 'object' && value !== null && !Array.isArray(value);
        default: return true;
    }
}

/**
 * The default a key should carry after its type is changed.
 *
 * Keeping a still-valid value matters more than it looks: the type control is
 * also what a person reaches for to fix a key whose type was unreadable, and
 * wiping on every change made that fix destroy the value it was meant to save.
 */
export function nextDefaultForType(current: unknown, newType: unknown): unknown {
    return isValidDefaultFor(newType, current) ? current : emptyDefaultFor(newType);
}
