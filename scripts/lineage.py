"""What a function makes its data from: the app's data types that flow into what it returns, followed back
through its local names to the parameters it was given and the calls that built them.

A data type is a dataclass or TypedDict the app defines. Building one with its constructor is not a step
of its own: the types its fields are filled from flow on into the result.
"""

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from scripts.decisions import FunctionNode

MUTATORS = {"append", "extend", "insert", "update", "add", "setdefault"}  # methods that fill a container
PLAIN_METHODS = {"staticmethod", "classmethod"}  # decorators after which `self` is not the instance

type Resolve = Callable[[ast.expr], list[str]]  # the package functions an expression calls


@dataclass(frozen=True)
class Typed:
    """An untyped call's result that a function names, by annotating the variable it keeps it in."""

    callee: str  # the function called, as module.function
    type: str  # the data type the annotation names


@dataclass(frozen=True)
class Transform:
    """What one function does to the app's data: the types it takes, builds its result from and returns."""

    label: str  # module.function, or module.function.<locals>.inner for a function nested in it, as Python names it
    takes: tuple[str, ...]  # data types its parameters name, the instance's own class first
    feeds: tuple[str, ...]  # data types that flow into what it returns
    gives: tuple[str, ...]  # data types it returns
    typed: tuple[Typed, ...] = ()  # untyped calls whose result it names as a data type


@dataclass
class Scope:
    """What one revision's package says about its functions, for following data through them."""

    types: set[str]  # the short names of the app's data types
    gives: dict[str, tuple[str, ...]] = field(default_factory=dict)  # each function's returned types


def named(node: ast.expr | None, types: set[str]) -> tuple[str, ...]:
    """The app's data types an annotation names, in order, each once."""
    if node is None:  # no annotation
        return ()
    text = ast.unparse(node).replace("'", "").replace('"', "")
    return tuple(dict.fromkeys(word for word in re.findall(r"\w+", text) if word in types))


def own(node: FunctionNode) -> Iterator[ast.AST]:
    """Every node of a function's own body; a nested function is yielded but not entered."""
    stack: list[ast.AST] = list(reversed(node.body))
    while stack:  # nodes left to visit
        sub = stack.pop()
        yield sub
        if not isinstance(
            sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):  # not a scope of its own
            stack.extend(reversed(list(ast.iter_child_nodes(sub))))


def nested(node: FunctionNode) -> list[FunctionNode]:
    """The functions defined inside a function's own body."""
    return [sub for sub in own(node) if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef)]


def roots(target: ast.expr) -> list[str]:
    """The names a target fills: `x`, `x.field` and `x[key]` fill x; `a, b` fills both."""
    if isinstance(target, ast.Name):  # a plain name
        return [target.id]
    if isinstance(target, ast.Attribute | ast.Subscript | ast.Starred):  # part of a value
        return roots(target.value)
    if isinstance(target, ast.Tuple | ast.List):  # several targets at once
        return [name for element in target.elts for name in roots(element)]
    return []


def filled(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """The targets one statement fills, each with the expression it fills them from: assignments, loops,
    `with`, and the methods that fill a container."""
    if isinstance(node, ast.Assign):  # `x = value`
        return [(target, node.value) for target in node.targets]
    # `x: T = value`, `x += value` or `(x := value)`
    if isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and node.value:
        return [(node.target, node.value)]
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):  # `for x in values`
        return [(node.target, node.iter)]
    if isinstance(node, ast.withitem) and node.optional_vars:  # `with value as x`
        return [(node.optional_vars, node.context_expr)]
    # `x.append(value)` and the like
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
        return [(node.func.value, value) for value in [*node.args, *(k.value for k in node.keywords)]]
    return []


def links(node: FunctionNode) -> dict[str, list[ast.expr]]:
    """Each local name of a function with every expression it is filled from."""
    found: dict[str, list[ast.expr]] = {}
    for sub in own(node):
        for target, value in filled(sub):
            for name in roots(target):
                found.setdefault(name, []).append(value)
    return found


def returned(node: FunctionNode) -> list[ast.expr]:
    """The expressions a function returns or yields."""
    return [
        sub.value
        for sub in own(node)
        if isinstance(sub, ast.Return | ast.Yield | ast.YieldFrom) and sub.value is not None
    ]


def parameters(node: FunctionNode, types: set[str], owner: str | None) -> dict[str, tuple[str, ...]]:
    """Each parameter with the data types its annotation names; `self` is its class when that is one."""
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    found = {a.arg: named(a.annotation, types) for a in args}
    plain = any(ast.unparse(d) in PLAIN_METHODS for d in node.decorator_list)
    if owner in types and args and not plain:  # a method: its first parameter is the instance
        found[args[0].arg] = (owner,)
    return found


def annotated(node: FunctionNode, types: set[str]) -> dict[str, tuple[str, ...]]:
    """Local names annotated with data types: `receipt: Receipt = ...` names what `receipt` holds."""
    return {
        sub.target.id: found
        for sub in own(node)
        if isinstance(sub, ast.AnnAssign)
        and isinstance(sub.target, ast.Name)
        and (found := named(sub.annotation, types))
    }


def made(sub: ast.AST, resolve: Resolve, scope: Scope, inner: dict[str, tuple[str, ...]]) -> list[str]:
    """The data types a call returns: a package function's, or a nested function's by its name."""
    if not isinstance(sub, ast.Call):  # not a call
        return []
    if isinstance(sub.func, ast.Name) and sub.func.id in inner:  # a function nested in this one
        return list(inner[sub.func.id])
    return [t for label in resolve(sub.func) for t in scope.gives.get(label, ())]


def feeds(
    node: FunctionNode,
    names: dict[str, tuple[str, ...]],
    resolve: Resolve,
    scope: Scope,
) -> tuple[str, ...]:
    """The data types that flow into what a function returns, from the names it was given or annotated,
    the calls whose results it keeps, and its nested functions."""
    bound = links(node)
    inner = {sub.name: named(sub.returns, scope.types) for sub in nested(node)}
    queue = returned(node)
    seen: set[str] = set()
    found: list[str] = []
    while queue:  # expressions still to follow back
        for sub in ast.walk(queue.pop(0)):
            found += made(sub, resolve, scope, inner)
            if isinstance(sub, ast.Name) and sub.id not in seen:  # a name not followed yet
                seen.add(sub.id)
                found += names.get(sub.id, ())
                queue += bound.get(sub.id, [])
    return tuple(dict.fromkeys(found))


def typed_calls(node: FunctionNode, resolve: Resolve, scope: Scope) -> tuple[Typed, ...]:
    """Calls that return untyped data the function then names: `pr: GitHubPull = _api(...)`."""
    found = []
    for sub in own(node):
        # an annotated name filled straight from a call
        if isinstance(sub, ast.AnnAssign) and isinstance(sub.value, ast.Call):
            callees = [label for label in resolve(sub.value.func) if not scope.gives.get(label)]
            found += [Typed(callee, t) for callee in callees for t in named(sub.annotation, scope.types)]
    return tuple(found)


def transform(label: str, node: FunctionNode, resolve: Resolve, scope: Scope, owner: str | None) -> Transform:
    """What one function does to the app's data."""
    params = parameters(node, scope.types, owner)
    takes = tuple(dict.fromkeys(t for found in params.values() for t in found))
    names = {**params, **annotated(node, scope.types)}
    gives = named(node.returns, scope.types)
    return Transform(
        label, takes, feeds(node, names, resolve, scope), gives, typed_calls(node, resolve, scope)
    )


def transforms(
    label: str, node: FunctionNode, resolve: Resolve, scope: Scope, owner: str | None
) -> list[Transform]:
    """A function and each function nested in it, as what they do to the app's data."""
    found = [transform(label, node, resolve, scope, owner)]
    found += [transform(f"{label}.<locals>.{sub.name}", sub, resolve, scope, None) for sub in nested(node)]
    return found
