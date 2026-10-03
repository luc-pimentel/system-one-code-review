"""What the app does with its data: a map of its commands and the files and services they touch, each
command's lineage from one data type to the next, the rules it keeps and the data it passes.
`--changed BASE` shows what a branch changed in all of that; `--readme` writes the map and the lineages
into README.md.

The app is the console script in pyproject.toml. Its package's `stores` module names every file the
app keeps and every service it calls, and a file's comment names the data type it holds. Anything else a
step touches shows as unnamed, and so does data that a new or edited function passes without named fields.
"""

import argparse
import ast
import copy
import html
import io
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from scripts import decisions, lineage, mermaid
from scripts.decisions import FunctionNode

CATALOG = "stores"
DEPTH = 6  # how many assignments and calls back a name is followed
READS = {"read_text", "read_bytes"}
WRITES = {"write_text", "write_bytes"}
WEB = {"get", "post", "put", "patch", "delete", "request", "stream"}
PROGRAMS = {"run", "check_output", "check_call", "call", "Popen"}
CONTAINERS = {"dict", "list", "tuple", "set", "frozenset"}
LOOSE = {"Any", "object"}
CONSTRUCTORS = ("__init__", "__post_init__", "__call__")  # what building an object runs, and calling it
MARK = {"new": "➕ ", "changed": "✏️ ", "same": ""}
WAYS = {"reads": "in", "writes": "out", "appends": "out", "calls": "call"}  # how an effect moves data
SHOWN_GAPS = 20  # unnamed things listed in full; the rest are counted
SHOWN_NAMES = 5  # names a line of the summary lists before it counts the rest
README = Path("README.md")
README_MARKS = ("<!-- flow -->", "<!-- /flow -->")  # the part of the README this script writes

type Origin = tuple[str, ast.expr]  # an expression, with the function it is written in


@dataclass(frozen=True)
class Store:
    """One file or service the catalog names."""

    label: str  # the catalog's name for it, e.g. stores.ROWS
    shown: str  # how the map shows it: a path, or the service's name
    holds: str  # the catalog's own words for what it holds
    service: bool  # a service the app calls, rather than a file it keeps
    type: str | None = None  # the data type a file holds, when its comment names one


@dataclass(frozen=True)
class Effect:
    """One thing a step does outside the program."""

    verb: str  # reads, writes, appends, calls or env
    target: str  # a store as the map shows it, an environment variable, or the code when unnamed
    named: bool = True  # whether the catalog, or the variable's own name, says what it is


@dataclass
class Site:
    """A call that may touch something outside the program, before it is named."""

    verb: str  # reads, writes, appends, calls, web or env
    nodes: tuple[ast.expr, ...]  # the expressions that say what it touches


@dataclass
class Step:
    """One function on a command's path, with what it touches, the data it makes and the rules it keeps."""

    label: str  # module.function
    depth: int  # calls between the command's handler and this function
    caller: str | None  # the function that first reaches it on this path
    doc: str  # the first line of its docstring
    shape: str  # the app's data types it takes and gives, as `(list[Row]) → Receipt`
    effects: list[Effect]  # the files, services and environment it touches
    rules: list[str]  # its refusals and early returns, in its own words
    text: str  # its source, to tell whether a branch changed it
    transforms: list[lineage.Transform]  # what it and the functions nested in it do to the data
    plain: list[str]  # annotations in its signature that pass data without naming its fields


@dataclass
class Command:
    """One subcommand of the app and the steps it runs, handler first."""

    name: str  # as typed, e.g. `s1cr run`
    about: str  # its help text
    steps: list[Step]  # every function it reaches, depth first

    def effects(self) -> list[Effect]:
        """Everything the command touches, each once, in the order its steps touch it."""
        return list(dict.fromkeys(effect for step in self.steps for effect in step.effects))


@dataclass
class Field:
    """One field of a data type."""

    name: str  # the field's name
    annotation: str  # its type, as written
    holds: str | None  # its comment: what it holds


@dataclass
class DataType:
    """A dataclass or TypedDict the app defines."""

    label: str  # module.Class
    kind: str  # dataclass or TypedDict
    doc: str  # the first line of its docstring
    fields: list[Field]  # its fields, in order
    text: str  # its source, to tell whether a branch changed it

    @property
    def name(self) -> str:
        """The class's own name, as signatures and the catalog's comments write it."""
        return self.label.rsplit(".", 1)[-1]

    def layout(self) -> list[tuple[str, str]]:
        """Its fields' names and types: what changes when its data does, comments aside."""
        return [(f.name, f.annotation) for f in self.fields]


@dataclass(frozen=True)
class Code:
    """One function's source at three depths, to tell what kind of change a branch made to it."""

    text: str  # as written
    logic: str  # its syntax tree without docstrings: its code and the types it names
    bare: str  # its syntax tree without docstrings or type annotations: its code alone


@dataclass
class App:
    """The app as one revision has it: its commands, data types, catalog and functions."""

    script: str  # the console script
    commands: list[Command]  # in the order they are registered
    types: dict[str, DataType]  # by label
    stores: dict[str, Store]  # by label; empty when the package has no catalog
    functions: dict[str, Code]  # every function and method of the package, by label

    def store_at(self, shown: str) -> Store | None:
        """The catalog name a step's effect points at, by how the map shows it."""
        return next((store for store in self.stores.values() if store.shown == shown), None)

    def named_type(self, name: str) -> DataType | None:
        """A data type by its own name."""
        return next((t for t in self.types.values() if t.name == name), None)


@dataclass(frozen=True)
class Gap:
    """Something new or edited code leaves unnamed."""

    where: str  # the function, field or step
    what: str  # what it leaves unnamed


@dataclass
class Changes:
    """How a branch changed one folder's functions, sorted by the kind of change each one got."""

    logic: list[str]  # functions whose code changed
    types: list[str]  # functions whose type annotations alone changed
    words: list[str]  # functions whose docstrings or comments alone changed
    new: list[str]  # functions added
    removed: list[str]  # functions removed


@dataclass
class Kept:
    """One function's rules, with every command whose path reaches it."""

    step: Step  # the function, as the first command to reach it has it
    commands: list[str]  # the commands that reach it, in the order they are registered


def qualified(graph: decisions.CallGraph, module: str, node: ast.AST) -> str:
    """What a name or `module.name` refers to across the package, as `module.name`, through the imports."""
    local = graph.imports.get(module, {})
    if isinstance(node, ast.Name):  # a name imported as itself, or defined in this module
        found = local.get(node.id)
        return found[1] if found and found[0] == "name" else f"{module}.{node.id}"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):  # `module.name`
        found = local.get(node.value.id)
        return f"{found[1]}.{node.attr}" if found and found[0] == "module" else ""
    return ""


def class_annotations(node: FunctionNode) -> Iterator[tuple[str, str]]:
    """Names a function and the functions nested in it annotate with a single class, with that class's
    name as written: `row: "Row"` and `pr: PullRequest | None` both count."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg) and sub.annotation:  # a parameter with a type
            pair = (sub.arg, sub.annotation)
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):  # an annotated local name
            pair = (sub.target.id, sub.annotation)
        else:
            continue
        written = re.sub(r"\s*\|\s*None$", "", annotation(pair[1]))
        if re.fullmatch(r"\w+", written):  # one name, not a container
            yield pair[0], written


class Graph(decisions.CallGraph):
    """The call graph, with `Class.method` and a typed name's `name.method` standing for that method alone,
    and building an object for its constructor alone."""

    def __init__(self, paths: list[Path]) -> None:
        self.classes: dict[str, ast.ClassDef] = {}
        self.scope: dict[str, str] = {}  # names in the function being read that hold a package class
        super().__init__(paths)

    def index(self, module: str, node: ast.stmt, prefix: str = "") -> None:
        """A function, or a class and its methods; a class is kept for the attributes its methods set."""
        if isinstance(node, ast.ClassDef):  # a class
            self.classes[f"{module}.{prefix}{node.name}"] = node
        super().index(module, node, prefix)

    def method(self, module: str, node: ast.Attribute) -> str:
        """The one method `Class.method` or `typed_name.method` names, or empty when neither is known."""
        receiver = node.value
        owner = self.scope.get(receiver.id, "") if isinstance(receiver, ast.Name) else ""
        owner = owner or qualified(self, module, receiver)
        label = f"{owner}.{node.attr}"
        return label if owner in self.classes and label in self.nodes else ""

    def resolve(self, module: str, node: ast.AST) -> list[str]:
        """The functions a name, `module.name`, `Class.method` or `typed_name.method` stands for; a class
        stands for what building one runs."""
        method = self.method(module, node) if isinstance(node, ast.Attribute) else ""
        if method:  # one method alone
            return [method]
        found = super().resolve(module, node)
        if qualified(self, module, node) in self.classes:  # a class: calling it builds an object
            return [label for label in found if label.rsplit(".", 1)[-1] in CONSTRUCTORS]
        return found

    def references(self, module: str, node: ast.AST) -> Iterator[str]:
        """Functions a node refers to, in evaluation order; a resolved attribute's parts name nothing more."""
        found = self.resolve(module, node) if isinstance(node, ast.Attribute) else []
        if found:  # `module.function` or `Class.method`
            yield from found
            return
        yield from super().references(module, node)

    def typed_names(self, label: str) -> dict[str, str]:
        """Names a function annotates with one of the package's classes, as that class's label; a method's
        `self` is its own class."""
        node, module = self.nodes[label], label.split(".")[0]
        owner = label.rsplit(".", 1)[0]
        found = {"self": owner} if owner in self.classes else {}
        for name, written in class_annotations(node):
            label_of = qualified(self, module, ast.Name(id=written))
            if label_of in self.classes:  # the annotation names one of the package's classes
                found[name] = label_of
        return found

    def edges(self, label: str) -> list[str]:
        """The functions one function refers to, the methods of its typed names among them."""
        self.scope = self.typed_names(label)
        return super().edges(label)

    def resolver(self, label: str) -> lineage.Resolve:
        """What the calls in function `label` stand for, the methods of its typed names among them."""
        self.scope = self.typed_names(label)
        module = label.split(".")[0]
        return lambda node: self.resolve(module, node)


class Resolver:
    """Names what an expression stands for on one command's path, as the catalog names it: followed back
    through local assignments, what callers pass, parameter defaults and the attributes a class sets."""

    def __init__(self, graph: Graph, catalog: dict[str, Store], reach: dict[str, list[str]]) -> None:
        self.graph, self.catalog, self.reach = graph, catalog, reach
        self.files = {file_name(store): store.label for store in catalog.values() if file_name(store)}
        self.locals: dict[str, dict[str, list[ast.expr]]] = {}

    def effects(self, label: str, node: FunctionNode) -> list[Effect]:
        """What one function touches outside the program, named as the catalog names it."""
        return list(dict.fromkeys(effect for place in sites(node) for effect in self.effect(label, place)))

    def effect(self, label: str, place: Site) -> list[Effect]:
        """The effects of one call, named; none when a lookup only looked like a web request."""
        if place.verb == "env":  # an environment variable, named by itself
            return [env(place)]
        named = [self.catalog[name] for name in sorted(self.union([(label, n) for n in place.nodes], 0))]
        if place.verb in ("calls", "web"):  # a program or a web service
            return [Effect("calls", store.shown) for store in named if store.service] or unnamed(place)
        return [Effect(place.verb, store.shown) for store in named if not store.service] or unnamed(place)

    def union(self, origins: list[Origin], depth: int) -> set[str]:
        """The catalog labels any of these expressions stands for, each read in its own function."""
        found: set[str] = set()
        found.update(*(self.names(context, node, depth) for context, node in origins))
        return found

    def names(self, label: str, node: ast.expr, depth: int) -> set[str]:
        """The catalog labels `node` stands for in function `label`: its own, else those it was built from."""
        found = self.mentioned(label, node)
        if found or depth >= DEPTH:  # named right here, or followed far enough
            return found
        given, defaults = self.origins(label, node)
        return self.union(given, depth + 1) or self.union(defaults, depth + 1)

    def mentioned(self, label: str, node: ast.expr) -> set[str]:
        """Catalog labels an expression names itself, or spells out as a file name the catalog gives."""
        module = label.split(".")[0]
        found = {
            name for sub in ast.walk(node) if (name := qualified(self.graph, module, sub)) in self.catalog
        }
        return found or {self.files[part] for part in spelled(node) if part in self.files}

    def origins(self, label: str, node: ast.expr) -> tuple[list[Origin], list[Origin]]:
        """What the names in an expression were built from, and the parameter defaults to fall back on."""
        given: list[Origin] = []
        defaults: list[Origin] = []
        for sub in ast.walk(node):
            # an attribute the object set on itself
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                given.extend(self.attribute(label, sub.attr))
            elif isinstance(sub, ast.Name) and sub.id != "self":  # a local name or a parameter
                found, default = self.binding(label, sub.id)
                given.extend(found)
                defaults.extend(default)
        return given, defaults

    def binding(self, label: str, name: str) -> tuple[list[Origin], list[Origin]]:
        """What a name in function `label` was given: its assignments, else what the caller passes for
        it as a parameter, with the parameter's default to fall back on."""
        node = self.graph.nodes.get(label)
        if node is None:  # not a function of the package
            return [], []
        assigned = self.assigned(label, node).get(name)
        if assigned:  # a local name
            return [(label, value) for value in assigned], []
        if name not in {
            a.arg for a in [*positional(label, node), *node.args.kwonlyargs]
        }:  # a global or a builtin
            return [], []
        fallback = default(node, name)
        return self.passed(label, name), [(label, fallback)] if fallback is not None else []

    def passed(self, label: str, name: str) -> list[Origin]:
        """What the caller on this command's path passes for parameter `name` of function `label`."""
        chain = self.reach.get(label) or []
        if not chain:  # the command's handler: nothing here calls it
            return []
        caller = chain[-1]
        resolve = self.graph.resolver(caller)
        params = [a.arg for a in positional(label, self.graph.nodes[label])]
        return [
            (caller, value)
            for call in ast.walk(self.graph.nodes[caller])
            if isinstance(call, ast.Call)
            and label in resolve(call.func)
            and (value := argument(call, params, name)) is not None
        ]

    def attribute(self, label: str, name: str) -> list[Origin]:
        """What the methods of `label`'s class assign to `self.<name>`, each read in its own method."""
        owner = label.rsplit(".", 1)[0]
        cls = self.graph.classes.get(owner)
        if cls is None:  # `label` is not a method
            return []
        return [
            (f"{owner}.{method.name}", value)
            for method in cls.body
            if isinstance(method, ast.FunctionDef)
            for target, value in pairs_in(method)
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == name
        ]

    def assigned(self, label: str, node: FunctionNode) -> dict[str, list[ast.expr]]:
        """Each local name of a function with every value it is given, read once per function."""
        if label not in self.locals:  # not read yet
            self.locals[label] = bindings(node)
        return self.locals[label]


def file_name(store: Store) -> str:
    """The file name a store's path ends in, when it names a file rather than a folder or a service."""
    last = store.shown.rsplit("/", 1)[-1]
    return last if not store.service and "." in last else ""


def spelled(node: ast.expr) -> set[str]:
    """The last part of every string in an expression: `"*/answers.jsonl"` spells answers.jsonl."""
    return {
        sub.value.rsplit("/", 1)[-1]
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    }


def bindings(node: ast.AST) -> dict[str, list[ast.expr]]:
    """Each name a function binds, with what it binds it to: assignments, loops, comprehensions, `with`."""
    found: dict[str, list[ast.expr]] = {}
    for target, value in pairs_in(node):
        if isinstance(target, ast.Name):  # a plain name, not an attribute or a subscript
            found.setdefault(target.id, []).append(value)
    return found


def pairs_in(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """Every target bound under `node`, with the expression it is bound to."""
    return [pair for sub in ast.walk(node) for pair in pairs(sub)]


def pairs(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """The targets one statement or clause binds, each with the expression it binds them to."""
    if isinstance(node, ast.Assign):  # `x = value`, or `x = y = value`
        return [(target, node.value) for target in node.targets]
    if isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value:  # `x: T = value`, or `(x := value)`
        return [(node.target, node.value)]
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):  # `for x in values`
        return [(node.target, node.iter)]
    if isinstance(node, ast.withitem) and node.optional_vars:  # `with value as x`
        return [(node.optional_vars, node.context_expr)]
    return []


def positional(label: str, node: FunctionNode) -> list[ast.arg]:
    """A function's positional parameters as callers fill them, a method's `self` left out."""
    found = [*node.args.posonlyargs, *node.args.args]
    return found[1:] if label.count(".") > 1 else found


def default(node: FunctionNode, name: str) -> ast.expr | None:
    """The default value of parameter `name`, if it has one."""
    args = node.args
    ordered = [*args.posonlyargs, *args.args]
    found = dict(
        zip([a.arg for a in ordered[len(ordered) - len(args.defaults) :]], args.defaults, strict=True)
    )
    found.update({a.arg: value for a, value in zip(args.kwonlyargs, args.kw_defaults, strict=True) if value})
    return found.get(name)


def argument(call: ast.Call, params: list[str], name: str) -> ast.expr | None:
    """The expression a call passes for parameter `name`, by position or by keyword."""
    index = params.index(name) if name in params else len(call.args)
    given = call.args[index] if index < len(call.args) else None
    keyword = next((k.value for k in call.keywords if k.arg == name), None)
    return given if given is not None and not isinstance(given, ast.Starred) else keyword


def sites(node: ast.AST) -> list[Site]:
    """Every method call under `node` that may touch something outside the program, in source order."""
    calls = sorted(
        (sub for sub in ast.walk(node) if isinstance(sub, ast.Call)), key=lambda c: (c.lineno, c.col_offset)
    )
    return [
        found
        for call in calls
        if isinstance(call.func, ast.Attribute) and (found := site(call, call.func)) is not None
    ]


def site(call: ast.Call, func: ast.Attribute) -> Site | None:
    """What one method call may touch outside the program, if anything."""
    receiver, name, first = func.value, func.attr, tuple(call.args[:1])
    owner = ast.unparse(receiver)
    if name in READS or name in WRITES:  # a whole file read or written at once
        return Site("reads" if name in READS else "writes", (receiver,))
    if name == "open":  # a file opened in a mode
        return Site(mode(call), (receiver,))
    if owner == "subprocess" and name in PROGRAMS:  # a program run
        return Site("calls", first)
    if owner in ("os.environ", "os") and name in ("get", "getenv"):  # an environment variable
        return Site("env", first)
    if name in WEB:  # a web request, or a lookup that only looks like one
        return Site("web", (receiver, *first))
    return None


def mode(call: ast.Call) -> str:
    """What opening a file does, from its mode: appends, writes or reads."""
    given = [*call.args[:1], *(k.value for k in call.keywords if k.arg == "mode")]
    text = str(given[0].value) if given and isinstance(given[0], ast.Constant) else "r"
    if "a" in text:  # appended to
        return "appends"
    return "writes" if "w" in text or "x" in text else "reads"


def env(place: Site) -> Effect:
    """An environment variable a step reads, by its own name when the code spells it out."""
    first = place.nodes[0] if place.nodes else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):  # spelled out
        return Effect("env", f"${first.value}")
    return Effect("env", ast.unparse(first) if first else "?", named=False)


def unnamed(place: Site) -> list[Effect]:
    """A call the catalog does not name; none when it was a lookup that only looked like a request."""
    shown = ast.unparse(place.nodes[0]) if place.nodes else "?"
    if place.verb == "web" and shown != "httpx":  # `x.get(...)` on a mapping, not a request
        return []
    return [Effect("calls" if place.verb == "web" else place.verb, shown, named=False)]


def read_catalog(path: Path, types: set[str]) -> dict[str, Store]:
    """The files and services the catalog module names, each with its own words for what it holds and
    the data type those words name."""
    if not path.exists():  # the package has no catalog
        return {}
    text = path.read_text()
    src = decisions.Source(text)
    known: dict[str, list[str]] = {}
    found = [catalog_entry(node, src, known) for node in ast.parse(text).body]
    return {store.label: replace(store, type=held(store, types)) for store in found if store}


def held(store: Store, types: set[str]) -> str | None:
    """The data type a file's comment names, as in `one Row per line`; None for a service or no type."""
    found = [word for word in re.findall(r"\b[A-Z]\w*", store.holds) if word in types]
    return found[0] if found and not store.service else None


def catalog_entry(node: ast.stmt, src: decisions.Source, known: dict[str, list[str]]) -> Store | None:
    """One catalog name: a constant with its comment, or a function that builds a path, with its docstring."""
    if isinstance(node, ast.FunctionDef):  # a path built from the function's arguments
        return built(node, known)
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:  # not one assignment
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Name):  # not a plain name
        return None
    label, holds = f"{CATALOG}.{target.id}", src.phrase(node.lineno, node.end_lineno or node.lineno) or ""
    if isinstance(node.value, ast.Constant):  # a service: a web address or a program
        return Store(label, target.id.lower().replace("_", " "), holds, service=True)
    known[target.id] = parts(node.value, known, set())
    return Store(label, "/".join(known[target.id]), holds, service=False)


def built(node: ast.FunctionDef, known: dict[str, list[str]]) -> Store | None:
    """A catalog function: the path it returns, with its parameters shown as `<name>`."""
    last = node.body[-1]
    if not isinstance(last, ast.Return) or last.value is None:  # it does not end by returning a path
        return None
    params = {a.arg for a in node.args.args}
    doc = decisions.first_sentence(ast.get_docstring(node) or "").rstrip(".")
    return Store(f"{CATALOG}.{node.name}", "/".join(parts(last.value, known, params)), doc, service=False)


def parts(node: ast.expr, known: dict[str, list[str]], params: set[str]) -> list[str]:
    """A path expression as its parts: literals as written, earlier catalog names by their parts,
    parameters as `<name>`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # `left / right`
        return parts(node.left, known, params) + parts(node.right, known, params)
    if isinstance(node, ast.Call) and node.args:  # `Path("...")`
        return parts(node.args[0], known, params)
    if isinstance(node, ast.Constant):  # a literal, maybe several parts long
        return str(node.value).split("/")
    if isinstance(node, ast.Name):  # a parameter, or a name the catalog gave earlier
        return [f"<{node.id}>"] if node.id in params else known.get(node.id, [node.id])
    return ["…"]


def data_types(path: Path, text: str) -> list[DataType]:
    """The dataclasses and TypedDicts one module defines, each field with what it holds."""
    src = decisions.Source(text)
    return [
        DataType(
            f"{path.stem}.{node.name}",
            kind,
            decisions.first_sentence(ast.get_docstring(node) or ""),
            fields(node, src),
            ast.get_source_segment(text, node) or "",
        )
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and (kind := data_kind(node))
    ]


def data_kind(node: ast.ClassDef) -> str:
    """`dataclass` or `TypedDict` when the class holds data that way, else empty."""
    if any("dataclass" in ast.unparse(d) for d in node.decorator_list):  # a dataclass
        return "dataclass"
    return "TypedDict" if any(ast.unparse(b).endswith("TypedDict") for b in node.bases) else ""


def fields(node: ast.ClassDef, src: decisions.Source) -> list[Field]:
    """A data type's fields in order, each with its comment."""
    return [
        Field(
            item.target.id,
            annotation(item.annotation),
            src.phrase(item.lineno, item.end_lineno or item.lineno),
        )
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    ]


def annotation(node: ast.expr) -> str:
    """An annotation as written, without the quotes of a forward reference."""
    return ast.unparse(node).replace("'", "").replace('"', "")


def shape(node: FunctionNode, types: set[str]) -> str:
    """The app's data types a function takes and gives, as `(list[Row]) → Receipt`; empty when it names none."""
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    takes = [annotation(a.annotation) for a in args if a.annotation and names_type(a.annotation, types)]
    gives = annotation(node.returns) if node.returns and names_type(node.returns, types) else ""
    shown = f"({', '.join(takes)})" if takes else ""
    return f"{shown} → {gives}".strip() if gives else shown


def names_type(node: ast.expr, types: set[str]) -> bool:
    """Whether an annotation names one of the app's data types."""
    return bool(set(re.findall(r"\w+", annotation(node))) & types)


def rules(fn: decisions.Function) -> list[str]:
    """A function's refusals and early returns, in its own words."""
    found = []
    for d in fn.decisions:
        said = d.phrase or (f"`{d.code}`" if d.code else d.kind)
        if d.outcome and d.outcome.startswith("✋"):  # it refuses
            found.append(f"✋ {d.phrase or d.outcome[2:]}")
        elif d.outcome and d.outcome.startswith("↩"):  # it returns early; a named outcome says how
            named = d.outcome[2:]
            found.append(f"↩ {said}" + ("" if named.startswith("returns") else f" → {named}"))
    return found


def helps(node: ast.AST) -> dict[str, str]:
    """Each subcommand's help text, from `add_parser("name", help="...")`."""
    found: dict[str, str] = {}
    for call in ast.walk(node):
        # not adding a subcommand
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "add_parser"
        ):
            continue
        name = call.args[0] if call.args else None
        text = next((k.value for k in call.keywords if k.arg == "help"), None)
        if isinstance(name, ast.Constant) and isinstance(text, ast.Constant):  # both spelled out
            found[str(name.value)] = str(text.value)
    return found


def stripped(node: FunctionNode, *, types: bool) -> str:
    """A function's syntax tree as text without its docstrings, and without type annotations unless `types`."""
    tree = copy.deepcopy(node)
    for sub in ast.walk(tree):
        # a function or class that opens with a docstring
        if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and ast.get_docstring(sub):
            sub.body = sub.body[1:] or [ast.Pass()]
        if not types:  # annotations go too
            unannotated(sub)
    return ast.dump(tree)


def unannotated(node: ast.AST) -> None:
    """Drop the type annotation a parameter, a function's return or an annotated name carries."""
    if isinstance(node, ast.arg):  # a parameter
        node.annotation = None
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):  # a function's return
        node.returns = None
    elif isinstance(node, ast.AnnAssign):  # `x: T = value`
        node.annotation = ast.Constant(None)


def codes(path: Path, text: str) -> dict[str, Code]:
    """Each function and method of one file, by label, at the three depths a change is told by."""
    return {
        f"{path.stem}.{name}": Code(
            ast.get_source_segment(text, node) or "", stripped(node, types=True), stripped(node, types=False)
        )
        for node, name in decisions.named_functions(ast.parse(text).body)
    }


@dataclass
class Package:
    """One revision's app package, read once: its call graph, functions, catalog and data types."""

    graph: Graph  # who calls whom
    functions: dict[str, decisions.Function]  # every function and method, by label
    catalog: dict[str, Store]  # the catalog's names, by label
    types: dict[str, DataType]  # the data types, by label
    scope: lineage.Scope  # the data types' names, and what each function returns

    def command(self, handler: str, name: str, about: str) -> Command:
        """One subcommand: every function its handler reaches but the catalog's own, as steps."""
        reach = self.graph.reach(handler)
        resolver = Resolver(self.graph, self.catalog, reach)
        steps = [
            self.step(label, chain, resolver)
            for label, chain in reach.items()
            if not label.startswith(f"{CATALOG}.")
        ]
        return Command(name, about, steps)

    def step(self, label: str, chain: list[str], resolver: Resolver) -> Step:
        """One function on a command's path, with its data, effects and rules."""
        node, fn = self.graph.nodes[label], self.functions.get(label)
        effects = resolver.effects(label, node)
        owner = label.split(".")[1] if label.count(".") == 2 else None
        made = lineage.transforms(label, node, self.graph.resolver(label), self.scope, owner)
        return Step(
            label=label,
            depth=len(chain),
            caller=chain[-1] if chain else None,
            doc=fn.doc if fn else "",
            shape=shape(node, self.scope.types),
            effects=effects,
            rules=rules(fn) if fn else [],
            text=fn.text if fn else "",
            transforms=made,
            plain=[annotation(a) for a in signature(label, node) if plain(a)],
        )


def build(root: Path) -> App | None:
    """The app under `root`: its console script's commands and their steps, its data types and catalog."""
    console = decisions.entry_point(root / "pyproject.toml")
    if console is None:  # no console script: no app to describe
        return None
    script, modules, function = console
    folder = next((p for p in (root / "src" / modules[0], root / modules[0]) if p.is_dir()), None)
    if folder is None:  # the package is not where the console script says
        return None
    paths = sorted(folder.glob("*.py"))
    texts = {path: path.read_text() for path in paths}
    graph = Graph(paths)
    types = {t.label: t for path in paths for t in data_types(path, texts[path])}
    names = {t.name for t in types.values()}
    package = Package(
        graph,
        {fn.label: fn for path in paths for fn in decisions.functions(path, texts[path])},
        read_catalog(folder / f"{CATALOG}.py", names),
        types,
        lineage.Scope(names, {label: lineage.named(n.returns, names) for label, n in graph.nodes.items()}),
    )
    entry = f"{modules[-1]}.{function}"
    about = helps(graph.nodes[entry]) if entry in graph.nodes else {}
    handlers = graph.handlers(entry) or [(entry, "")]
    commands = [package.command(h, f"{script} {name}".strip(), about.get(name, "")) for h, name in handlers]
    functions = {label: code for path in paths for label, code in codes(path, texts[path]).items()}
    return App(script, commands, types, package.catalog, functions)


def escape(text: str) -> str:
    """Text safe in markdown: angle brackets outside code spans escaped, so `<run>` is not taken for HTML."""
    pieces = text.split("`")
    return "`".join(html.escape(p, quote=False) if i % 2 == 0 else p for i, p in enumerate(pieces))


def cell(text: str) -> str:
    """Text safe in a table cell: escaped, its pipes kept from splitting the row."""
    return escape(text).replace("|", "\\|")


def code(text: str) -> str:
    """Text as a code span in a table cell."""
    return f"`{text}`".replace("|", "\\|")


def summary(text: str) -> str:
    """Text for a `<summary>`, where markdown does not render: escaped, its code spans as `<code>`."""
    return re.sub(r"`([^`]*)`", r"<code>\1</code>", html.escape(text, quote=False))


def plural(count: int, word: str) -> str:
    """A count with its noun, as `1 function` or `3 functions`."""
    return f"{count} {word}" + ("s" if count != 1 else "")


def listed(names: list[str]) -> str:
    """Names as code spans, the first few in full and the rest counted."""
    shown = ", ".join(f"`{name}`" for name in names[:SHOWN_NAMES])
    more = len(names) - SHOWN_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


def short(label: str) -> str:
    """A function's own name, as an arrow says it: `jev.run.<locals>.ask` is ask, `github.Comparer.__call__`
    is Comparer."""
    *rest, last = label.split(".")
    return rest[-1] if last.startswith("__") and rest else last


def store_node(store: Store) -> mermaid.Node:
    """A catalog name as a box: a file with the data type it holds, or a service."""
    ident = mermaid.ident(store.label.split(".", 1)[-1].lower())
    if store.service:  # a service the app calls
        return mermaid.Node(f"s_{ident}", store.shown, "service")
    kept = f"<br/><i>{store.type}</i>" if store.type else ""
    return mermaid.Node(f"f_{ident}", store.shown + kept, "file")


def target_node(effect: Effect, app: App) -> mermaid.Node:
    """The box for what an effect touches: its catalog name's, or a 🟡 box when the catalog does not name it."""
    store = app.store_at(effect.target)
    if store is not None:  # the catalog names it
        return store_node(store)
    kind = "service" if effect.verb == "calls" else "file"
    return mermaid.Node(f"u_{mermaid.ident(effect.target)}", f"🟡 {effect.target}", kind)


def data_node(name: str) -> mermaid.Node:
    """A data type as a box."""
    return mermaid.Node(f"t_{mermaid.ident(name)}", name, "data")


def command_node(cmd: Command) -> mermaid.Node:
    """A command as a box."""
    return mermaid.Node(f"c_{mermaid.ident(cmd.name.split()[-1])}", cmd.name, "step")


def touch(command: str, other: str, way: set[str], writers: dict[str, str]) -> mermaid.Edge:
    """The arrow between a command and one thing it touches; a file both read and written points away
    from the first command that writes it, so the map keeps the pipeline's order."""
    if "call" in way:  # a service
        return mermaid.Edge(other, command, kind="call")
    if way == {"in"}:  # read only
        return mermaid.Edge(other, command)
    first = writers.setdefault(other, command) == command
    if way == {"out"}:  # written only
        return mermaid.Edge(command, other)
    return mermaid.Edge(command, other, kind="both") if first else mermaid.Edge(other, command, kind="both")


def command_edges(chart: mermaid.Chart, cmd: Command, app: App, writers: dict[str, str]) -> None:
    """Draw one command with an arrow to or from each file and service it touches; a command that writes
    nothing prints its result."""
    me = command_node(cmd)
    chart.add(me)
    ways: dict[str, set[str]] = {}
    for effect in cmd.effects():
        if effect.verb in WAYS:  # a file or a service, not the environment
            node = target_node(effect, app)
            chart.add(node)
            ways.setdefault(node.id, set()).add(WAYS[effect.verb])
    chart.edges += [touch(me.id, ident, way, writers) for ident, way in ways.items()]
    if not any("out" in way for way in ways.values()):  # nothing written: the result is printed
        chart.add(mermaid.Node("t_out", "terminal", "end"))
        chart.edges.append(mermaid.Edge(me.id, "t_out"))


def pipeline(app: App) -> mermaid.Chart:
    """The map: every command with the files it reads and writes and the services it calls."""
    chart = mermaid.Chart()
    writers: dict[str, str] = {}
    for cmd in app.commands:
        command_edges(chart, cmd, app, writers)
    return chart


def comparable(base: App | None) -> bool:
    """Whether a base revision names its files and services, so its charts can be compared with a branch's."""
    return bool(base and base.stores)


def pipeline_chart(head: App, base: App | None) -> mermaid.Chart:
    """The map; against a base that names its files and services, what a branch changed is marked."""
    now = pipeline(head)
    return mermaid.compare(now, pipeline(base)) if base and comparable(base) else now


def detour(out: dict[str, set[str]], source: str, target: str) -> bool:
    """Whether `target` can be reached from `source` through at least one other box."""
    seen = out.get(source, set()) - {target}
    stack = list(seen)
    while stack:  # boxes left to look past
        reached = out.get(stack.pop(), set())
        if target in reached:  # reached another way
            return True
        fresh = reached - seen
        seen |= fresh
        stack += fresh
    return False


def reduced(edges: list[mermaid.Edge]) -> list[mermaid.Edge]:
    """The arrows left once every arrow a longer path already draws is dropped."""
    out: dict[str, set[str]] = {}
    for edge in edges:
        out.setdefault(edge.source, set()).add(edge.target)
    return [edge for edge in edges if not detour(out, edge.source, edge.target)]


def holders(name: str, drawn: list[str], app: App) -> list[tuple[str, str]]:
    """The data types drawn with a field that holds data type `name`, each with that field's name."""
    found = []
    for other in drawn:
        held_by = app.named_type(other)
        found += [
            (other, f.name)
            for f in (held_by.fields if held_by and other != name else [])
            if name in re.findall(r"\w+", f.annotation)
        ]
    return found


def contained(nodes: list[mermaid.Node], edges: list[mermaid.Edge], app: App) -> list[mermaid.Edge]:
    """Dotted arrows for data that moves only inside other data: a type nothing here makes comes out of
    the type holding it, and a type nothing here takes goes into it."""
    made = {e.target for e in edges}
    taken = {e.source for e in edges}
    joined = {(e.source, e.target) for e in edges} | {(e.target, e.source) for e in edges}
    drawn = [n.label for n in nodes if n.shape == "data"]
    found = []
    for name in drawn:
        ident = data_node(name).id
        for holder, field_name in holders(name, drawn, app):
            if (ident, data_node(holder).id) in joined:  # an arrow already joins the two
                continue
            if ident not in made:  # it arrives inside its holder
                found.append(mermaid.Edge(data_node(holder).id, ident, f".{field_name}", "part"))
            elif ident not in taken:  # it leaves inside its holder
                found.append(mermaid.Edge(ident, data_node(holder).id, f".{field_name}", "part"))
    return found


class Lineage:
    """One command's lineage: its data types, files and services, joined by the functions that turn one
    into the next. An arrow says which functions carry it."""

    def __init__(self, app: App, cmd: Command) -> None:
        self.app, self.cmd = app, cmd
        self.steps = {step.label: step for step in cmd.steps}
        self.nodes: dict[str, mermaid.Node] = {}
        self.carried: dict[tuple[str, str], list[str]] = {}  # each arrow, with the functions that carry it

    def link(self, source: mermaid.Node, target: mermaid.Node, via: str) -> None:
        """An arrow from one box to another, carried by function `via`."""
        if source.id == target.id:  # a function that gives back the type it was given
            return
        self.nodes.setdefault(source.id, source)
        self.nodes.setdefault(target.id, target)
        carriers = self.carried.setdefault((source.id, target.id), [])
        if short(via) not in carriers:  # this function is not on the arrow yet
            carriers.append(short(via))

    def transform(self, t: lineage.Transform, services: list[mermaid.Node]) -> None:
        """Arrows from the types a function makes its result from to the types it returns; through the
        services it calls, when its result comes back from one."""
        sources = [data_node(f) for f in t.feeds if f not in t.gives]
        targets = [data_node(g) for g in t.gives]
        if services and targets:  # the result comes back from a service
            hops = [(s, service) for service in services for s in sources]
            hops += [(service, g) for service in services for g in targets]
        else:
            hops = [(s, g) for s in sources for g in targets]
        for source, target in hops:
            self.link(source, target, t.label)

    def effect(self, effect: Effect, main: lineage.Transform, services: list[mermaid.Node]) -> None:
        """Arrows for one file a step reads or writes: to or from the data type it holds; to what the step
        returns, or from what it was given or called, when the file names no type."""
        store = self.app.store_at(effect.target)
        if store is None or store.service or WAYS.get(effect.verb) not in ("in", "out"):  # not a named file
            return
        box = store_node(store)
        named = [data_node(store.type)] if store.type else []
        if WAYS[effect.verb] == "in":  # the file's data comes in
            hops = [(box, t) for t in named or [data_node(g) for g in main.gives]]
        else:
            given = named or [data_node(t) for t in main.feeds or main.takes] or services
            hops = [(s, box) for s in given]
        for source, target in hops:
            self.link(source, target, main.label)

    def typed(self, typed: lineage.Typed) -> None:
        """Arrows from the services an untyped call reaches and the files it reads to the data type its
        result is named."""
        callee = self.steps.get(typed.callee)
        for effect in callee.effects if callee else []:
            store = self.app.store_at(effect.target)
            if store and (store.service or effect.verb == "reads"):  # where the untyped result came from
                self.link(store_node(store), data_node(typed.type), typed.callee)

    def step(self, step: Step) -> None:
        """The arrows one step draws: its functions' data, the files it reads and writes, the services it calls."""
        services = [target_node(e, self.app) for e in step.effects if e.verb == "calls"]
        for t in step.transforms:
            self.transform(t, services if t.label == step.label else [])
            for typed in t.typed:
                self.typed(typed)
        for effect in step.effects:
            self.effect(effect, step.transforms[0], services)

    def chart(self) -> mermaid.Chart:
        """The lineage as a flowchart: every arrow a longer path already draws dropped, and dotted arrows
        for data that moves inside other data."""
        for step in self.cmd.steps:
            self.step(step)
        edges = reduced([mermaid.Edge(s, t, ", ".join(via)) for (s, t), via in self.carried.items()])
        nodes = list(self.nodes.values())
        return mermaid.Chart(nodes, edges + contained(nodes, edges, self.app))


def lineage_chart(cmd: Command, app: App, old: Command | None, base: App | None) -> mermaid.Chart:
    """One command's lineage; against a comparable base, what a branch changed is marked, a data type
    whose fields changed among it."""
    now = Lineage(app, cmd).chart()
    if not base or not comparable(base):  # nothing to compare with
        return now
    chart = mermaid.compare(now, Lineage(base, old).chart() if old else mermaid.Chart())
    reshaped = {t.name for t in app.types.values() if t.layout() != layout_of(base, t.label)}
    chart.nodes = [
        replace(n, mark="changed") if n.mark == "same" and n.shape == "data" and n.label in reshaped else n
        for n in chart.nodes
    ]
    return chart


def layout_of(app: App, label: str) -> list[tuple[str, str]]:
    """A data type's fields and their types in one revision; empty when it has no such type."""
    found = app.types.get(label)
    return found.layout() if found else []


def unnamed_on(cmd: Command) -> list[str]:
    """What a command's steps pass without naming its fields, as `swrbench.load: list[dict]`."""
    return [f"{step.label}: {written}" for step in cmd.steps for written in step.plain]


def lineage_block(cmd: Command, chart: mermaid.Chart, *, opened: bool, note: str = "") -> str:
    """One command's lineage in a fold: what it is for, the chart, and what its steps leave unnamed."""
    title = f"<b><code>{html.escape(cmd.name)}</code></b>" + (f" — {summary(cmd.about)}" if cmd.about else "")
    gaps = unnamed_on(cmd)
    footer = f"🟡 Unnamed here, so drawn without it: {listed(gaps)}" if gaps else ""
    drawn = mermaid.render(chart) if chart.nodes else "No named data moves through this command."
    body = "\n\n".join(part for part in (drawn, footer) if part)
    return f"<details{' open' if opened else ''}><summary>{title}{note}</summary>\n\n{body}\n\n</details>"


def stores_section(head: App, base: App | None) -> str:
    """The catalog's names a branch added, changed or removed; with no base, all of them."""
    before = base.stores if base else {}
    rows = [
        store_row(store, before.get(label), base is not None)
        for label, store in head.stores.items()
        if base is None or before.get(label) != store
    ]
    rows += [f"| ➖ | `{label}` | | | | |" for label in before if label not in head.stores]
    if not rows:  # the catalog did not change
        return ""
    header = ["| | name | path or service | kind | holds | type |", "| --- | --- | --- | --- | --- | --- |"]
    return "\n".join(["**Files and services**", "", *header, *rows])


def store_row(store: Store, old: Store | None, diff: bool) -> str:
    """One catalog name as a table row."""
    mark = ("new" if old is None else "changed") if diff else "same"
    holds = cell(store.holds) if store.holds else "🟡 no comment"
    kind = "service" if store.service else ("file" if file_name(store) else "folder")
    held_type = f"`{store.type}`" if store.type else ""
    return (
        f"| {MARK[mark].strip()} | `{store.label}` | {code(store.shown)} | {kind} | {holds} | {held_type} |"
    )


def types_section(head: App, base: App | None) -> str:
    """The data types a branch added, changed or removed, field by field; with no base, all of them."""
    before = base.types if base else {}
    blocks = [
        type_block(t, before.get(label), base is not None)
        for label, t in head.types.items()
        if base is None or label not in before or before[label].text != t.text
    ]
    blocks += [f"➖ `{label}` removed" for label in before if label not in head.types]
    if not blocks:  # no data type changed
        return ""
    return "\n\n".join(["**Data**", *blocks])


def type_block(t: DataType, old: DataType | None, diff: bool) -> str:
    """One data type as a folded table of its fields; added, changed and removed fields marked."""
    was = {f.name: f for f in old.fields} if old else {}
    rows = [field_row(f, was.get(f.name), old is not None) for f in t.fields]
    rows += [f"| ➖ | `{name}` | | |" for name in was if name not in {f.name for f in t.fields}]
    mark = ("new" if old is None else "changed") if diff else "same"
    title = f"{MARK[mark]}<b><code>{t.label}</code></b> {t.kind}" + (f" · {summary(t.doc)}" if t.doc else "")
    table = "\n".join(["| | field | type | holds |", "| --- | --- | --- | --- |", *rows])
    return f"<details><summary>{title}</summary>\n\n{table}\n\n</details>"


def field_row(f: Field, old: Field | None, compared: bool) -> str:
    """One field as a table row, marked when it is new or changed."""
    mark = ("new" if old is None else ("changed" if old != f else "same")) if compared else "same"
    holds = cell(f.holds) if f.holds else "🟡 no comment"
    return f"| {MARK[mark].strip()} | `{f.name}` | {code(f.annotation)} | {holds} |"


def rule_book(app: App) -> dict[str, Kept]:
    """Every function on a command's path that keeps rules, once, in the order the commands reach them."""
    book: dict[str, Kept] = {}
    for cmd in app.commands:
        for step in cmd.steps:
            if step.rules:  # it refuses something or returns early
                book.setdefault(step.label, Kept(step, [])).commands.append(cmd.name)
    return book


def kept_line(kept: Kept) -> str:
    """A function that keeps rules, as one line: what it passes and touches, and the commands reaching it."""
    step = kept.step
    touches = ", ".join(f"{e.verb} `{e.target}`" for e in step.effects)
    reached = ", ".join(f"`{name}`" for name in kept.commands)
    bits = [f"`{step.label}`", f"`{step.shape}`" if step.shape else "", escape(touches), f"in {reached}"]
    return "- " + " · ".join(bit for bit in bits if bit)


def rules_section(app: App) -> str:
    """Every rule the app keeps, each once, under the function that keeps it."""
    book = rule_book(app)
    lines = [
        line
        for kept in book.values()
        for line in [kept_line(kept), *(f"  - {escape(rule)}" for rule in kept.step.rules)]
    ]
    count = sum(len(kept.step.rules) for kept in book.values())
    title = f"<b>Rules</b> — {count} refusals and early returns in {len(book)} functions, each once"
    return f"<details><summary>{title}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def rule_changes(head: App, base: App) -> list[tuple[Kept, list[str]]]:
    """Each function whose rules a branch changed, with its rules marked new or removed."""
    before, after = rule_book(base), rule_book(head)
    found = []
    for label, kept in after.items():
        was = before[label].step.rules if label in before else []
        now = kept.step.rules
        marked = [f"➕ {escape(r)}" for r in now if r not in was]
        marked += [f"➖ ~~{escape(r)}~~" for r in was if r not in now]
        if marked:  # a rule came or went
            found.append((kept, marked))
    gone = [kept for label, kept in before.items() if label not in after]
    return found + [(kept, [f"➖ ~~{escape(r)}~~" for r in kept.step.rules]) for kept in gone]


def rules_diff(changes: list[tuple[Kept, list[str]]]) -> str:
    """The rules a branch added or removed, under the functions that keep them."""
    if not changes:  # no rule came or went
        return ""
    lines = [line for kept, marks in changes for line in [kept_line(kept), *(f"  - {m}" for m in marks)]]
    title = f"<b>Rules</b> — {counted([m for _, marks in changes for m in marks])}"
    return f"<details open><summary>{title}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def classify(before: dict[str, Code], after: dict[str, Code]) -> Changes:
    """Each function a branch added, removed or edited, by what the edit changed: its code, only its type
    annotations, or only its docstrings and comments."""
    found = Changes([], [], [], [label for label in after if label not in before], [])
    found.removed = [label for label in before if label not in after]
    for label, now in after.items():
        was = before.get(label)
        if was is None or was.text == now.text:  # new, or untouched
            continue
        if was.bare != now.bare:  # its code changed
            found.logic.append(label)
        elif was.logic != now.logic:  # only its annotations changed
            found.types.append(label)
        else:
            found.words.append(label)
    return found


def changes_line(found: Changes) -> str:
    """How a branch changed a folder's functions, in words; empty when it changed none."""
    said = []
    if found.logic:  # some functions' code changed
        said.append(f"changed in {plural(len(found.logic), 'function')} ({listed(found.logic)})")
    if found.types:  # some changed only their annotations
        said.append(f"only type annotations in {len(found.types)}")
    if found.words:  # some changed only their words
        said.append(f"only docstrings or comments in {len(found.words)}")
    if found.new:  # some functions are new
        said.append(f"{len(found.new)} new ({listed(found.new)})")
    if found.removed:  # some functions are gone
        said.append(f"{len(found.removed)} removed ({listed(found.removed)})")
    return "; ".join(said)


def counted(marks: list[str]) -> str:
    """How many rules came and went, as `2 new, 1 removed`; a count of none is left out."""
    added = sum(mark.startswith("➕") for mark in marks)
    said = [f"{added} new" if added else "", f"{len(marks) - added} removed" if len(marks) > added else ""]
    return ", ".join(part for part in said if part)


def folder_codes(root: Path, folder: str) -> dict[str, Code]:
    """Every function of one folder's Python files, by label."""
    if not (root / folder).is_dir():  # no such folder in this revision
        return {}
    return {
        label: found
        for path in decisions.python_files([str(root / folder)])
        for label, found in codes(path, path.read_text()).items()
    }


def edge_words(edge: mermaid.Edge, names: dict[str, str]) -> str:
    """One arrow a branch added or removed on the map, as `s1cr run now writes data/log.txt`."""
    command, other = (
        (edge.source, edge.target) if edge.source.startswith("c_") else (edge.target, edge.source)
    )
    verb = {"call": "calls", "both": "reads and writes"}.get(edge.kind, "")
    verb = verb or ("writes" if edge.source == command else "reads")
    verb = "prints its result" if other == "t_out" else f"{verb} `{names[other]}`"
    when = "now" if edge.mark == "new" else "no longer"
    return f"`{names[command]}` {when} {verb}"


def pipeline_news(chart: mermaid.Chart, head: App, base: App) -> list[str]:
    """What a branch changed on the map, in words: commands added or removed, what each one now touches
    or no longer does, and the catalog's names added, removed or changed."""
    names = {n.id: re.sub(r"<br/>.*", "", n.label) for n in chart.nodes}
    said = [
        f"`{n.label}` is {n.mark}" for n in chart.nodes if n.shape == "step" and n.mark in ("new", "removed")
    ]
    said += [edge_words(e, names) for e in chart.edges if e.mark in ("new", "removed")]
    added = [label for label in head.stores if label not in base.stores]
    gone = [label for label in base.stores if label not in head.stores]
    moved = [label for label, s in head.stores.items() if label in base.stores and base.stores[label] != s]
    groups = (
        ("new in the catalog", added),
        ("gone from the catalog", gone),
        ("changed in the catalog", moved),
    )
    return said + [f"{word}: {listed(names)}" for word, names in groups if names]


def data_news(head: App, base: App) -> list[str]:
    """The data types a branch added, removed or reshaped, in words; a comment's wording is no change."""
    added = [label for label in head.types if label not in base.types]
    gone = [label for label in base.types if label not in head.types]
    said = [f"{word}: {listed(names)}" for word, names in (("new", added), ("gone", gone)) if names]
    for label, t in head.types.items():
        was = base.types.get(label)
        if was and was.layout() != t.layout():  # its fields or their types changed
            added = [f"+`{n}`" for n, _ in t.layout() if n not in dict(was.layout())]
            gone = [f"−`{n}`" for n, _ in was.layout() if n not in dict(t.layout())]
            moved = [f"~`{n}`" for n, a in t.layout() if dict(was.layout()).get(n, a) != a]
            said.append(f"`{label}` ({' '.join(added + gone + moved)})")
    return said


def sentence(said: list[str]) -> str:
    """Several things a branch changed, as one line: the first few, the rest counted."""
    if not said:  # nothing changed
        return "unchanged."
    more = len(said) - SHOWN_NAMES
    return "; ".join(said[:SHOWN_NAMES]) + (f"; and {more} more" if more > 0 else "") + "."


def summary_lines(
    head: App, base: App, pipe: mermaid.Chart, charts: dict[str, mermaid.Chart], outside: list[str]
) -> str:
    """What a branch changed in the app, one line each: its map, its data, each command's lineage, its
    rules and its code; and outside it, in the other folders."""
    flows = [name for name, chart in charts.items() if chart.marked()]
    rule_marks = [m for _, marks in rule_changes(head, base) for m in marks]
    lines = [
        f"- **Map:** {sentence(pipeline_news(pipe, head, base))}",
        f"- **Data:** {sentence(data_news(head, base))}",
        f"- **Lineage:** {'changed in ' + listed(flows) + ', drawn below.' if flows else 'unchanged.'}",
        f"- **Rules:** {counted(rule_marks) + ', listed below.' if rule_marks else 'unchanged.'}",
        f"- **Code:** {changes_line(classify(base.functions, head.functions)) or 'unchanged'}.",
    ]
    return "\n".join(lines + outside)


def outside_lines(base_root: Path, head_root: Path, folders: Iterable[str], app_folder: str) -> list[str]:
    """How a branch changed the functions of each folder that is not the app's."""
    lines = []
    for folder in folders:
        found = classify(folder_codes(base_root, folder), folder_codes(head_root, folder))
        said = changes_line(found) if folder != app_folder else ""
        if said:  # the branch changed this folder's functions
            lines.append(f"- **`{folder}/`, outside the app:** {said}.")
    return lines


def plain(node: ast.expr) -> bool:
    """Whether an annotation passes data without naming it: a bare container, or values typed Any or object."""
    inner = {id(sub.value) for sub in ast.walk(node) if isinstance(sub, ast.Subscript)}
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Name) and sub.id in CONTAINERS and id(sub) not in inner
        ):  # a container, its contents untyped
            return True
        if isinstance(sub, ast.Subscript) and loose_values(sub):  # a mapping whose values are Any or object
            return True
    return False


def loose_values(node: ast.Subscript) -> bool:
    """`dict[str, Any]`, `dict[str, object]` and their like: keys named, values not."""
    elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else []
    mapping = ast.unparse(node.value) in ("dict", "Mapping")
    return mapping and len(elements) == 2 and ast.unparse(elements[1]) in LOOSE


def function_gaps(label: str, node: FunctionNode) -> list[Gap]:
    """A new or edited function with no docstring, or passing data without named fields."""
    found = []
    # nothing says what it is for, and it is not a dunder method
    if not ast.get_docstring(node) and not (node.name.startswith("__") and node.name.endswith("__")):
        found.append(Gap(label, "has no docstring"))
    notes = signature(label, node)
    return found + [Gap(label, f"passes `{annotation(a)}`, which names no fields") for a in notes if plain(a)]


def signature(label: str, node: FunctionNode) -> list[ast.expr]:
    """The annotations of a function's parameters, a method's `self` left out, then of what it returns."""
    args = [*positional(label, node), *node.args.kwonlyargs]
    return [a.annotation for a in args if a.annotation] + ([node.returns] if node.returns else [])


def file_gaps(path: Path, text: str, before: str) -> list[Gap]:
    """The gaps in one file's new and edited functions and data types."""
    old = {
        name: ast.get_source_segment(before, node)
        for node, name in decisions.named_functions(ast.parse(before).body)
    }
    found = [
        gap
        for node, name in decisions.named_functions(ast.parse(text).body)
        if old.get(name) != ast.get_source_segment(text, node)
        for gap in function_gaps(f"{path.stem}.{name}", node)
    ]
    was = {t.label: t.text for t in data_types(path, before)}
    return found + [
        Gap(f"{t.label}.{f.name}", "has no comment saying what it holds")
        for t in data_types(path, text)
        if was.get(t.label) != t.text
        for f in t.fields
        if not f.holds
    ]


def folder_gaps(base_root: Path, head_root: Path, folder: str) -> list[Gap]:
    """The gaps in one folder's new and edited functions and data types."""
    if not (head_root / folder).is_dir():  # the folder is not there
        return []
    found = []
    for path in decisions.python_files([str(head_root / folder)]):
        old = base_root / path.relative_to(head_root)
        found += file_gaps(path, path.read_text(), old.read_text() if old.exists() else "")
    return found


def step_gaps(head: App | None, base: App | None) -> list[Gap]:
    """Unnamed effects of the steps a branch added or edited; with no base, of every step."""
    before = {s.label: s.text for c in base.commands for s in c.steps} if base else {}
    return [
        Gap(s.label, f"{e.verb} `{e.target}`, which the catalog does not name")
        for c in (head.commands if head else [])
        for s in c.steps
        if before.get(s.label) != s.text
        for e in s.effects
        if not e.named
    ]


def gaps_section(found: list[Gap]) -> str:
    """The gaps as a list: the first ones in full, the rest counted."""
    if not found:  # everything new or edited names its data
        return ""
    lines = [f"- `{g.where}` {g.what}" for g in found[:SHOWN_GAPS]]
    if len(found) > SHOWN_GAPS:  # more than fit
        lines.append(f"- … and {len(found) - SHOWN_GAPS} more")
    return "\n".join(["🟡 **Still unnamed in what this branch added or edited**", "", *lines])


def map_block(chart: mermaid.Chart, title: str, *, legend: bool) -> str:
    """The map under a heading, with the colours' legend when it marks what a branch changed."""
    return "\n\n".join([title, mermaid.render(chart), *([mermaid.LEGEND] if legend else [])])


def lineage_charts(head: App, base: App | None) -> dict[str, mermaid.Chart]:
    """Each command's lineage by name; against a comparable base, what a branch changed in it marked."""
    before = {c.name: c for c in base.commands} if base else {}
    return {c.name: lineage_chart(c, head, before.get(c.name), base) for c in head.commands}


def changed(base_root: Path, head_root: Path, folders: Iterable[str] = ("src", "scripts")) -> tuple[str, int]:
    """What a branch changed in the app, as markdown, and how many things new or edited code leaves unnamed."""
    head, base = build(head_root), build(base_root)
    folders = list(folders)
    found = [g for folder in folders for g in folder_gaps(base_root, head_root, folder)]
    found = list(dict.fromkeys(found + step_gaps(head, base)))
    if head is None:  # no app to show
        return gaps_section(found), len(found)
    base = base or App(head.script, [], {}, {}, {})
    charts = lineage_charts(head, base)
    pipe = pipeline_chart(head, base)
    flows = {c.name: c for c in head.commands if charts[c.name].marked()}
    folds = [lineage_block(cmd, charts[name], opened=len(flows) <= 2) for name, cmd in flows.items()]
    outside = outside_lines(base_root, head_root, folders, "src")
    # read top down and cut from the bottom: the data folds go first, the summary and the map never
    sections = [
        f"### What this branch changes in `{head.script}`",
        summary_lines(head, base, pipe, charts, outside),
        map_block(
            pipe,
            "**Map** — every command, the files it reads and writes, the services it calls",
            legend=pipe.marked(),
        ),
        *folds,
        gaps_section(found),
        stores_section(head, base),
        rules_diff(rule_changes(head, base)),
        types_section(head, base),
    ]
    return "\n\n".join(s for s in sections if s), len(found)


def whole(root: Path) -> str:
    """The whole app: the map, its files and services, each command's lineage, its rules, its data."""
    app = build(root)
    if app is None:  # no console script
        return "No console script in pyproject.toml."
    shown = [
        f"### How `{app.script}` works",
        map_block(
            pipeline(app),
            "**Map** — every command, the files it reads and writes, the services it calls",
            legend=False,
        ),
        stores_section(app, None),
        "**Lineage** — what each command makes of the data, function by function",
        *(lineage_block(c, Lineage(app, c).chart(), opened=False) for c in app.commands),
        rules_section(app),
        types_section(app, None),
    ]
    return "\n\n".join(s for s in shown if s)


def readme_part(root: Path) -> str:
    """The README's part this script writes: the map, then each command's lineage."""
    app = build(root)
    if app is None:  # no console script
        return ""
    shown = [
        mermaid.render(pipeline(app)),
        *(lineage_block(c, Lineage(app, c).chart(), opened=False) for c in app.commands),
    ]
    return "\n\n".join(shown)


def with_readme_part(text: str, part: str) -> str:
    """README text with the part between the flow markers replaced; unchanged when it has no markers."""
    start, end = README_MARKS
    if start not in text or end not in text:  # nowhere to write the part
        return text
    before, rest = text.split(start, 1)
    return f"{before}{start}\n{part}\n{end}{rest.split(end, 1)[1]}"


def checkout(base: str, into: Path, paths: Iterable[str] = ("pyproject.toml", "src", "scripts")) -> Path:
    """The base revision's project files, extracted into `into`; the working tree is left alone."""
    present = set(decisions.git("ls-tree", "--name-only", base).split())
    wanted = [path for path in paths if path in present]
    if not wanted:  # nothing to extract, or no such revision
        return into
    archive = subprocess.run(["git", "archive", base, *wanted], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main(argv: list[str] | None = None) -> int:
    """Print the whole app, or with `--changed` what a branch changed in it; `--readme` writes the map and
    the lineages into README.md."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", metavar="BASE", help="only what changed since this revision")
    parser.add_argument("--readme", action="store_true", help="write the map and lineages into README.md")
    args = parser.parse_args(argv)
    if args.readme:  # refresh the README's part
        README.write_text(with_readme_part(README.read_text(), readme_part(Path.cwd())))
        return 0
    if not args.changed:  # the whole app
        print(whole(Path.cwd()))
        return 0
    with tempfile.TemporaryDirectory() as folder:
        print(changed(checkout(args.changed, Path(folder)), Path.cwd())[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
