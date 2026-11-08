/**
 * What an expression may call (If / Else, Switch, a Loop's collection, while and break) — F957.
 *
 * The engine's `core.expressions.vocabulary()`, written here as JSON: the editor needs it before any request, and
 * `backend/tests/unit/test_expressions_offer_what_people_write.py` fails the moment the two differ, so this list
 * is never a second opinion. Change the engine's tables, then paste its new `vocabulary()` here.
 */
export const EXPRESSION_VOCABULARY = {"functions": ["abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len", "list", "max", "min", "reversed", "round", "sorted", "str", "sum"], "methods": {"str": ["capitalize", "casefold", "count", "endswith", "find", "index", "isalnum", "isalpha", "isdigit", "islower", "isnumeric", "isspace", "isupper", "join", "lower", "lstrip", "partition", "removeprefix", "removesuffix", "replace", "rfind", "rindex", "rpartition", "rsplit", "rstrip", "split", "splitlines", "startswith", "strip", "title", "upper"], "dict": ["get", "items", "keys", "values"], "list": ["count", "index"], "tuple": ["count", "index"]}} as const;

/** How each receiver's methods are introduced. */
export const RECEIVER_WORDS: Record<string, string> = {
    str: 'On text',
    dict: 'On a dict',
    list: 'On a list',
    tuple: 'On a tuple',
};
