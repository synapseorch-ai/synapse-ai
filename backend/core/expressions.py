"""The small expression dialect that `if_else`, `switch` and `loop` steps speak.

Those steps each carry an expression written either by a person in the step
editor or by the builder model through `add_single_step`. Until this module
existed the strings went to CPython's `eval()` with `{'__builtins__': {}}` as
their only containment, which is not a sandbox in either direction:
`().__class__.__bases__[0].__subclasses__()` walks from an inline literal to
`Popen`, and a value pulled out of shared state reaches the same graph from the
other side — `state.note.__class__` needed no literal at all.

So an expression is parsed to a syntax tree and walked here instead.
`ast.parse(…, mode="eval")` is the only compile-family call in this file and it
returns a tree, never a code object; `eval`, `exec` and `compile()`-to-code are
not used and must not be added. Nothing is enforced by a list of dangerous
names, either: the walker knows the node types, the names and the methods it is
willing to evaluate and refuses everything else, so a construct nobody thought
of is refused by default rather than admitted by default.

The dialect, which is what every existing expression was written against:

    state.result.flag == True        dot navigation; a missing key is None
    state.items[0]                   indexing, because lists come back raw
    (state.n or 0) < 5               and / or / not, comparisons, arithmetic
    str(state.status).lower()        the eleven functions and the method table
    state.tag in ('a', 'b')          membership, literals, `x if y else z`

Refused, each with a reason a person can act on: f-strings, comprehensions,
lambdas and generator expressions, the walrus, bit operators, any name that is
not `state` or one of the eleven, any attribute beginning with an underscore,
and any method outside the table. `str.format` is excluded deliberately — the
attribute path in `"{0.__class__}".format(x)` lives inside a string where no
syntax tree can see it. So are the mutating methods: `state.items.append(x)`
used to rewrite persisted state from inside a routing condition.

There is no wall-clock timeout, and that is a decision rather than an omission.
With no comprehension, lambda or generator there is no unbounded-iteration
construct left in the grammar; the node cap and the three arithmetic guards
below close the remaining ways to spend real time. A timeout would need a
thread or `signal` — unusable off the main thread under FastAPI — and could not
preempt a single CPython bigint operation anyway.
"""

from __future__ import annotations

import ast
import functools
import operator
from dataclasses import dataclass
from typing import Any


class ExpressionRefused(ValueError):
    """The expression uses a construct this evaluator will not run.

    Raised from the parse-and-check pass, before any value exists — which is
    what lets `unsafe_reason()` answer at authoring time, with no run.
    """


@dataclass(frozen=True)
class ExprResult:
    """`refused` separates "we will not run this" from "this blew up".

    Both end the same way at every call site — a warning and the step's
    documented default — but only the first is worth telling an author about
    while they are still writing it.
    """

    value: Any
    error: str | None = None
    refused: bool = False


class DotNavigableState:
    """Shared state, navigable with dots. A missing key is None, not an error.

    Moved here from `core.orchestration.steps` when the evaluator stopped being
    `eval()`. The semantics are deliberately unchanged, because every expression
    anyone has written was written against them — including the ones that are
    arguably wrong, like `state.d.keys()` failing because `keys` is looked up as
    a state key first.
    """

    def __init__(self, data: dict):
        object.__setattr__(self, '_data', data)

    def __getattr__(self, name: str):
        data = object.__getattribute__(self, '_data')
        if name.startswith('_'):
            raise AttributeError(name)
        val = data.get(name)
        if isinstance(val, dict):
            return DotNavigableState(val)
        return val

    def __getitem__(self, key):
        data = object.__getattribute__(self, '_data')
        val = data.get(key)
        if isinstance(val, dict):
            return DotNavigableState(val)
        return val

    def __iter__(self):
        # Without this, `list(state)` falls back to the old sequence protocol,
        # calling __getitem__(0), __getitem__(1), … — each of which returns None
        # instead of raising IndexError, so the iteration never terminates. A
        # dict's own meaning (iterate the keys) is both the obvious one and a
        # finite one.
        return iter(object.__getattribute__(self, '_data'))

    def __repr__(self):
        return repr(object.__getattribute__(self, '_data'))

    def __str__(self):
        return str(object.__getattribute__(self, '_data'))

    def __eq__(self, other):
        data = object.__getattribute__(self, '_data')
        if isinstance(other, DotNavigableState):
            return data == object.__getattribute__(other, '_data')
        return data == other

    def __bool__(self):
        return bool(object.__getattribute__(self, '_data'))

    def __contains__(self, item):
        return item in object.__getattribute__(self, '_data')

    def __len__(self):
        return len(object.__getattribute__(self, '_data'))


def _raw(value: Any) -> Any:
    """Unwrap, so a wrapper can never escape into shared state.

    `switch` writes its expression's value to `output_key`, and shared state is
    `json.dumps`-ed at every checkpoint — so `switch_expression: "state.llm_json"`
    (one keystroke from a real one) used to park an unserialisable object in the
    run and fail the checkpoint several steps later.
    """
    if isinstance(value, DotNavigableState):
        return object.__getattribute__(value, '_data')
    return value


# ── What the dialect contains ────────────────────────────────────────────────

_STATE = 'state'

#: The eleven callables, carried over unchanged. The `'None'/'True'/'False'`
#: entries the old namespace also held were dead weight: since 3.8 those parse
#: to `ast.Constant` and are never looked up as names.
_FUNCS = {
    'len': len, 'int': int, 'float': float, 'str': str,
    'bool': bool, 'list': list, 'dict': dict, 'abs': abs,
    'min': min, 'max': max, 'round': round,
}

#: Keyed by (exact type, name) and holding the *unbound* function, which is what
#: makes this a boundary rather than a suggestion: an object that merely has a
#: `.get` is not a dict and finds no entry, and a `str` subclass that overrides
#: `lower` does not get to run its override. Nothing here calls `getattr` on a
#: value, so no `__getattr__` or descriptor in shared state can fire either.
_METHODS: dict[tuple[type, str], Any] = {
    (t, name): getattr(t, name)
    for t, names in (
        (str, (
            'lower upper strip lstrip rstrip startswith endswith split rsplit '
            'join replace count find title capitalize casefold isdigit isalpha '
            'isnumeric removeprefix removesuffix'
        ).split()),
        (dict, ('get', 'keys', 'values', 'items')),
        (list, ('index', 'count')),
        (tuple, ('index', 'count')),
    )
    for name in names
}

_METHOD_NAMES = frozenset(name for _, name in _METHODS)

_ALLOWED_NODES = frozenset({
    ast.Expression, ast.Load, ast.Subscript, ast.Slice, ast.IfExp,
    ast.Tuple, ast.List, ast.Set,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
})

_NODE_NAMES = {
    ast.Lambda: 'a lambda', ast.ListComp: 'a list comprehension',
    ast.SetComp: 'a set comprehension', ast.DictComp: 'a dict comprehension',
    ast.GeneratorExp: 'a generator expression', ast.comprehension: 'a comprehension',
    ast.JoinedStr: 'an f-string', ast.FormattedValue: 'an f-string',
    ast.NamedExpr: 'a walrus assignment', ast.Starred: 'argument unpacking',
    ast.Await: 'await', ast.Yield: 'yield', ast.YieldFrom: 'yield from',
    ast.LShift: '<<', ast.RShift: '>>', ast.BitOr: '|', ast.BitAnd: '&',
    ast.BitXor: '^', ast.Invert: '~', ast.MatMult: '@',
}

_LOOP_HINT = ' — filter in a transform step and loop over what it writes'
_HINTS = {
    ast.JoinedStr: ' — build the text with str(…) and +',
    ast.FormattedValue: ' — build the text with str(…) and +',
    ast.ListComp: _LOOP_HINT, ast.SetComp: _LOOP_HINT,
    ast.DictComp: _LOOP_HINT, ast.GeneratorExp: _LOOP_HINT,
}

#: Naveen's one live expression is 5 nodes; the longest shipped template is ~20.
#: The depth cap also keeps the recursive walker away from `RecursionError`.
_MAX_NODES = 200
_MAX_DEPTH = 25

#: `10 ** 10**10` and `'x' * 10**9` are each one token of memory exhaustion, and
#: `'%1000000000d' % 1` allocates a gigabyte — the three ways left to spend real
#: time once iteration is gone. String `%` also re-enters `__getitem__` on its
#: right operand, so it is numbers only.
_MAX_POW_EXP = 64
_MAX_SEQ_REPEAT = 100_000


def _describe(kind: type) -> str:
    return _NODE_NAMES.get(kind, kind.__name__)


# ── The static pass: everything that can be decided without a value ──────────

def _check(node: ast.AST, depth: int, count: list[int], *, func_pos: bool = False) -> None:
    count[0] += 1
    if count[0] > _MAX_NODES:
        raise ExpressionRefused(f'the expression has more than {_MAX_NODES} parts')
    if depth > _MAX_DEPTH:
        raise ExpressionRefused(f'the expression is nested more than {_MAX_DEPTH} deep')

    kind = type(node)

    if kind is ast.Name:
        if func_pos:
            if node.id not in _FUNCS:
                raise ExpressionRefused(
                    f'{node.id}(…) is not one of the functions available here '
                    f'({", ".join(sorted(_FUNCS))})')
        elif node.id != _STATE:
            extra = f'; {node.id}(…) is available as a function' if node.id in _FUNCS else ''
            raise ExpressionRefused(
                f"'{node.id}' is not a name an expression can read — only `state` is{extra}")
        return

    if kind is ast.Attribute:
        if node.attr.startswith('_'):
            raise ExpressionRefused(
                f"'.{node.attr}' is not readable — an attribute beginning with an "
                f'underscore is never available to an expression')
        if func_pos and node.attr not in _METHOD_NAMES:
            raise ExpressionRefused(f'.{node.attr}(…) is not one of the methods available here')
        _check(node.value, depth + 1, count)
        return

    if kind is ast.Call:
        if node.keywords:
            raise ExpressionRefused('keyword arguments are not allowed')
        if not isinstance(node.func, (ast.Name, ast.Attribute)):
            raise ExpressionRefused('only a named function or a method may be called')
        _check(node.func, depth + 1, count, func_pos=True)
        for arg in node.args:
            _check(arg, depth + 1, count)
        return

    if kind is ast.Constant:
        if node.value is not None and not isinstance(node.value, (str, int, float, bool)):
            raise ExpressionRefused(f'{type(node.value).__name__} literals are not allowed')
        return

    if kind is ast.Dict:
        # `{**other}` parses as a key of None, which `iter_child_nodes` skips —
        # so it has to be caught here or it reaches the walker unchecked.
        if any(key is None for key in node.keys):
            raise ExpressionRefused('dictionary unpacking is not allowed')

    elif kind not in _ALLOWED_NODES:
        raise ExpressionRefused(
            f'{_describe(kind)} is not allowed in an expression{_HINTS.get(kind, "")}')

    for child in ast.iter_child_nodes(node):
        _check(child, depth + 1, count)


@functools.lru_cache(maxsize=256)
def _compiled(expr: str) -> tuple[ast.Expression | None, str | None, bool]:
    """`(tree, error, refused)` — the tree is None whenever an error is set.

    Cached because `loop_while_condition` is re-evaluated on every iteration up
    to `loop_max_iterations`, so the steady-state cost of an expression has to
    be a dict lookup rather than a parse.
    """
    try:
        tree = ast.parse(expr, mode='eval')
    except SyntaxError as exc:
        # A typo is not a refusal. It reaches the caller as the same runtime
        # error string `eval()` used to produce.
        return None, str(exc), False
    try:
        _check(tree, 0, [0])
    except ExpressionRefused as exc:
        return None, str(exc), True
    return tree, None, False


# ── The walk: everything that needs a value ──────────────────────────────────

def _guard_pow(base: Any, exp: Any) -> None:
    if isinstance(base, int) and isinstance(exp, int) and exp > _MAX_POW_EXP:
        raise ValueError(f'** is limited to an exponent of {_MAX_POW_EXP} here')


def _guard_repeat(left: Any, right: Any) -> None:
    for seq, n in ((left, right), (right, left)):
        if isinstance(seq, (str, list, tuple)) and isinstance(n, int):
            if len(seq) * n > _MAX_SEQ_REPEAT:
                raise ValueError(
                    f'repeating a sequence past {_MAX_SEQ_REPEAT} items is not allowed here')


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}

_CMPOPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class _Walk:
    __slots__ = ('state',)

    def __init__(self, state: DotNavigableState):
        self.state = state

    def run(self, tree: ast.Expression) -> Any:
        return self._eval(tree.body)

    def _eval(self, node: ast.AST) -> Any:
        handler = _DISPATCH.get(type(node))
        if handler is None:
            # `_check` has already refused anything without a handler. This is
            # the backstop: a node type added to `_ALLOWED_NODES` without an
            # evaluator fails loudly instead of silently doing something else.
            raise ExpressionRefused(f'{_describe(type(node))} cannot be evaluated')
        return handler(self, node)

    def _constant(self, node):
        return node.value

    def _name(self, node):
        if node.id == _STATE:
            return self.state
        raise ExpressionRefused(f"'{node.id}' is not a name an expression can read")

    def _attribute(self, node):
        receiver = self._eval(node.value)
        if isinstance(receiver, DotNavigableState):
            # Delegated on purpose: missing-key-is-None and the nested rewrap are
            # the wrapper's, not a second copy of them that can drift.
            return getattr(receiver, node.attr)
        raise AttributeError(
            f"'{type(receiver).__name__}' object has no attribute '{node.attr}'")

    def _subscript(self, node):
        return self._eval(node.value)[self._eval(node.slice)]

    def _slice(self, node):
        return slice(
            self._eval(node.lower) if node.lower is not None else None,
            self._eval(node.upper) if node.upper is not None else None,
            self._eval(node.step) if node.step is not None else None,
        )

    def _call(self, node):
        func = node.func
        if type(func) is ast.Name:
            fn = _FUNCS.get(func.id)
            if fn is None:
                raise ExpressionRefused(f'{func.id}(…) is not available here')
            return fn(*[self._eval(a) for a in node.args])
        receiver = self._eval(func.value)
        fn = _METHODS.get((type(receiver), func.attr))
        if fn is None:
            raise TypeError(
                f"'{type(receiver).__name__}' object has no method "
                f"'{func.attr}' available to an expression")
        return fn(receiver, *[self._eval(a) for a in node.args])

    def _boolop(self, node):
        if type(node.op) is ast.And:
            value: Any = True
            for operand in node.values:
                value = self._eval(operand)
                if not value:
                    return value
            return value
        value = False
        for operand in node.values:
            value = self._eval(operand)
            if value:
                return value
        return value

    def _unaryop(self, node):
        value = self._eval(node.operand)
        kind = type(node.op)
        if kind is ast.Not:
            return not value
        if kind is ast.USub:
            return -value
        return +value

    def _binop(self, node):
        left = self._eval(node.left)
        right = self._eval(node.right)
        kind = type(node.op)
        if kind is ast.Pow:
            _guard_pow(left, right)
        elif kind is ast.Mult:
            _guard_repeat(left, right)
        elif kind is ast.Mod and not (
            isinstance(left, (int, float)) and isinstance(right, (int, float))
        ):
            raise TypeError('% is only available between numbers')
        return _BINOPS[kind](left, right)

    def _compare(self, node):
        left = self._eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self._eval(comparator)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True

    def _ifexp(self, node):
        return self._eval(node.body) if self._eval(node.test) else self._eval(node.orelse)

    def _tuple(self, node):
        return tuple(_raw(self._eval(e)) for e in node.elts)

    def _list(self, node):
        return [_raw(self._eval(e)) for e in node.elts]

    def _set(self, node):
        return {_raw(self._eval(e)) for e in node.elts}

    def _dict(self, node):
        return {
            self._eval(k): _raw(self._eval(v))
            for k, v in zip(node.keys, node.values)
        }


_DISPATCH = {
    ast.Constant: _Walk._constant, ast.Name: _Walk._name,
    ast.Attribute: _Walk._attribute, ast.Subscript: _Walk._subscript,
    ast.Slice: _Walk._slice, ast.Call: _Walk._call,
    ast.BoolOp: _Walk._boolop, ast.UnaryOp: _Walk._unaryop,
    ast.BinOp: _Walk._binop, ast.Compare: _Walk._compare,
    ast.IfExp: _Walk._ifexp, ast.Tuple: _Walk._tuple,
    ast.List: _Walk._list, ast.Set: _Walk._set, ast.Dict: _Walk._dict,
}


# ── The three questions callers ask ──────────────────────────────────────────

def check_expression(expr: str | None) -> str | None:
    """`None` if the expression can be evaluated, else one sentence saying why not.

    Pure syntax — it needs no state, which is what makes it usable while someone
    is still writing the step. Reports typos as well as refusals; an empty
    expression is not this function's business, because every caller already
    reports a missing one in its own words.
    """
    if not expr or not expr.strip():
        return None
    return _compiled(expr)[1]


def unsafe_reason(expr: str | None) -> str | None:
    """`None` unless the expression is *refused* — the hard gate.

    A merely invalid expression is not refused here: refusing a typo at the
    point of writing would stop a builder tool call for something the engine
    already survives. `check_expression` is the advisory that reports those.
    """
    if not expr or not expr.strip():
        return None
    _, error, refused = _compiled(expr)
    return error if refused else None


def evaluate_expression(expr: str | None, state: dict, default: Any = None) -> ExprResult:
    """Evaluate `expr` against shared state. Never raises."""
    if not expr or not expr.strip():
        return ExprResult(default, 'the expression is empty')
    tree, error, refused = _compiled(expr)
    if tree is None:
        return ExprResult(default, error, refused)
    try:
        value = _Walk(DotNavigableState(state)).run(tree)
    except ExpressionRefused as exc:
        return ExprResult(default, str(exc), True)
    except Exception as exc:
        return ExprResult(default, str(exc), False)
    return ExprResult(_raw(value), None, False)
