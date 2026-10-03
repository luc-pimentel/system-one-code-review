"""What each command of the app does, step by step: the data it passes, the files and services it
touches, and the rules it keeps. `--changed BASE` shows what a branch changed in all of that.

The app is the console script in pyproject.toml. Its package's `stores` module names every file the
app keeps and every service it calls. Anything else a step touches shows as unnamed, and so does data
that a new or edited function passes without named fields.
"""

import argparse
import ast
import html
import io
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from scripts import decisions
from scripts.decisions import FunctionNode

CATALOG = "stores"
DEPTH = 6  # how many assignments and calls back a name is followed
READS = {"read_text", "read_bytes"}
WRITES = {"write_text", "write_bytes"}
WEB = {"get", "post", "put", "patch", "delete", "request", "stream"}
PROGRAMS = {"run", "check_output", "check_call", "call", "Popen"}
CONTAINERS = {"dict", "list", "tuple", "set", "frozenset"}
LOOSE = {"Any", "object"}
MARK = {"new": "➕ ", "changed": "✏️ ", "same": ""}
COLUMNS = {"reads": ("reads", "env"), "calls": ("calls",), "writes": ("writes", "appends")}
SHOWN_GAPS = 20  # unnamed things listed in full; the rest are counted

type Origin = tuple[str, ast.expr]  # an expression, with the function it is written in


@dataclass(frozen=True)
class Store:
    """One file or service the catalog names."""

    label: str  # the catalog's name for it, e.g. stores.ROWS
    shown: str  # how the map shows it: a path, or the service's name
    holds: str  # the catalog's own words for what it holds
    service: bool  # a service the app calls, rather than a file it keeps


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
    """One function on a command's path, with what it touches and the rules it keeps."""

    label: str  # module.function
    depth: int  # calls between the command's handler and this function
    caller: str | None  # the function that first reaches it on this path
    doc: str  # the first line of its docstring
    shape: str  # the app's data types it takes and gives, as `(list[Row]) → Receipt`
    effects: list[Effect]  # the files, services and environment it touches
    rules: list[str]  # its refusals and early returns, in its own words
    text: str  # its source, to tell whether a branch changed it


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


@dataclass
class App:
    """The app as one revision has it: its commands, data types and catalog."""

    script: str  # the console script
    commands: list[Command]  # in the order they are registered
    types: dict[str, DataType]  # by label
    stores: dict[str, Store]  # by label; empty when the package has no catalog


@dataclass(frozen=True)
class Gap:
    """Something new or edited code leaves unnamed."""

    where: str  # the function, field or step
    what: str  # what it leaves unnamed


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


class Graph(decisions.CallGraph):
    """The call graph, with `Class.method` standing for that method alone, and the classes it read."""

    def __init__(self, paths: list[Path]) -> None:
        self.classes: dict[str, ast.ClassDef] = {}
        super().__init__(paths)

    def index(self, module: str, node: ast.stmt, prefix: str = "") -> None:
        """A function, or a class and its methods; a class is kept for the attributes its methods set."""
        if isinstance(node, ast.ClassDef):  # a class
            self.classes[f"{module}.{prefix}{node.name}"] = node
        super().index(module, node, prefix)

    def resolve(self, module: str, node: ast.AST) -> list[str]:
        """The functions a name, `module.name` or `Class.method` stands for."""
        owner = qualified(self, module, node.value) if isinstance(node, ast.Attribute) else ""
        method = f"{owner}.{node.attr}" if owner in self.classes and isinstance(node, ast.Attribute) else ""
        if method in self.nodes:  # `Class.method`: that method alone, not the whole class
            return [method]
        return super().resolve(module, node)

    def references(self, module: str, node: ast.AST) -> Iterator[str]:
        """Functions a node refers to, in evaluation order; a resolved attribute's parts name nothing more."""
        found = self.resolve(module, node) if isinstance(node, ast.Attribute) else []
        if found:  # `module.function` or `Class.method`
            yield from found
            return
        yield from super().references(module, node)


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
        module = caller.split(".")[0]
        params = [a.arg for a in positional(label, self.graph.nodes[label])]
        return [
            (caller, value)
            for call in ast.walk(self.graph.nodes[caller])
            if isinstance(call, ast.Call)
            and label in self.graph.resolve(module, call.func)
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


def read_catalog(path: Path) -> dict[str, Store]:
    """The files and services the catalog module names, each with its own words for what it holds."""
    if not path.exists():  # the package has no catalog
        return {}
    text = path.read_text()
    src = decisions.Source(text)
    known: dict[str, list[str]] = {}
    found = [catalog_entry(node, src, known) for node in ast.parse(text).body]
    return {store.label: store for store in found if store}


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


@dataclass
class Package:
    """One revision's app package, read once: its call graph, functions, catalog and data types."""

    graph: Graph  # who calls whom
    functions: dict[str, decisions.Function]  # every function and method, by label
    catalog: dict[str, Store]  # the catalog's names, by label
    types: dict[str, DataType]  # the data types, by label

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
        return Step(
            label=label,
            depth=len(chain),
            caller=chain[-1] if chain else None,
            doc=fn.doc if fn else "",
            shape=shape(node, {label.rsplit(".", 1)[-1] for label in self.types}),
            effects=resolver.effects(label, node),
            rules=rules(fn) if fn else [],
            text=fn.text if fn else "",
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
    package = Package(
        graph,
        {fn.label: fn for path in paths for fn in decisions.functions(path, texts[path])},
        read_catalog(folder / f"{CATALOG}.py"),
        {t.label: t for path in paths for t in data_types(path, texts[path])},
    )
    entry = f"{modules[-1]}.{function}"
    about = helps(graph.nodes[entry]) if entry in graph.nodes else {}
    handlers = graph.handlers(entry) or [(entry, "")]
    commands = [package.command(h, f"{script} {name}".strip(), about.get(name, "")) for h, name in handlers]
    return App(script, commands, package.types, package.catalog)


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


def touched(cmd: Command, old: Command) -> bool:
    """Whether a branch changed anything on a command's path: a step added, removed or edited."""
    return {s.label: s.text for s in cmd.steps} != {s.label: s.text for s in old.steps}


def map_table(head: App, base: App | None) -> str:
    """Every command with what it reads, calls and writes; with a base, what a branch touched is marked."""
    before = {c.name: c for c in base.commands} if base else {}
    compare = bool(base and base.stores)
    lines = ["| | command | reads | calls | writes |", "| --- | --- | --- | --- | --- |"]
    lines += [map_row(c, before.get(c.name), base is not None, compare) for c in head.commands]
    lines += [f"| ➖ | `{name}` | | | |" for name in before if name not in {c.name for c in head.commands}]
    return "\n".join(lines)


def map_row(cmd: Command, old: Command | None, diff: bool, compare: bool) -> str:
    """One command's row; new things it touches are marked when the base names them too."""
    mark = "same"
    if diff:  # a branch is being compared
        mark = "new" if old is None else ("changed" if touched(cmd, old) else "same")
    known = set(old.effects()) if old and compare else set(cmd.effects())
    cells = {column: targets(cmd.effects(), verbs, known) for column, verbs in COLUMNS.items()}
    writes = cells["writes"] or "stdout"
    return f"| {MARK[mark].strip()} | `{cmd.name}` | {cells['reads']} | {cells['calls']} | {writes} |"


def targets(effects: list[Effect], verbs: tuple[str, ...], known: set[Effect]) -> str:
    """The targets of the effects with these verbs, each once; new ones and unnamed ones marked."""
    shown = dict.fromkeys(
        ("➕ " if e not in known else "") + ("🟡 " if not e.named else "") + code(e.target)
        for e in effects
        if e.verb in verbs
    )
    return ", ".join(shown)


def stores_section(head: App, base: App | None) -> str:
    """The catalog's names a branch added, changed or removed; with no base, all of them."""
    before = base.stores if base else {}
    rows = [
        store_row(store, before.get(label), base is not None)
        for label, store in head.stores.items()
        if base is None or before.get(label) != store
    ]
    rows += [f"| ➖ | `{label}` | | | |" for label in before if label not in head.stores]
    if not rows:  # the catalog did not change
        return ""
    header = ["| | name | path or service | kind | holds |", "| --- | --- | --- | --- | --- |"]
    return "\n".join(["**Files and services**", "", *header, *rows])


def store_row(store: Store, old: Store | None, diff: bool) -> str:
    """One catalog name as a table row."""
    mark = ("new" if old is None else "changed") if diff else "same"
    holds = cell(store.holds) if store.holds else "🟡 no comment"
    kind = "service" if store.service else ("file" if file_name(store) else "folder")
    return f"| {MARK[mark].strip()} | `{store.label}` | {code(store.shown)} | {kind} | {holds} |"


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


def commands_section(head: App, base: App | None) -> str:
    """Each command a branch touched, step by step; with no base, every command with all its rules."""
    before = {c.name: c for c in base.commands} if base else {}
    compare = bool(base and base.stores)
    chosen = [c for c in head.commands if base is None or c.name not in before or touched(c, before[c.name])]
    return "\n\n".join(
        command_block(c, before.get(c.name), base is not None, compare=compare, unfold=len(chosen) == 1)
        for c in chosen
    )


def command_block(cmd: Command, old: Command | None, diff: bool, *, compare: bool, unfold: bool) -> str:
    """One command's steps in a fold whose summary says what changed."""
    before = {s.label: s for s in old.steps} if old else {}
    marks = {s.label: step_mark(s, before, diff) for s in cmd.steps}
    shown = visible(cmd.steps, marks, full=not diff)
    lines = [
        line
        for s in cmd.steps
        if s.label in shown
        for line in step_lines(
            s, before.get(s.label), marks[s.label], compare, rules_too=not diff or marks[s.label] != "same"
        )
    ]
    gone = [label for label in before if label not in marks]
    lines += [f"- ➖ `{label}`" for label in gone]
    tally = Counter(marks.values()) + Counter({"removed": len(gone)})
    said = ", ".join(f"{tally[word]} {word}" for word in ("new", "changed", "removed") if tally[word])
    mark = ("new" if old is None else "changed") if diff else "same"
    title = f"{MARK[mark]}<b><code>{html.escape(cmd.name)}</code></b>"
    title += (f" — {summary(cmd.about)}" if cmd.about else "") + (f" · steps {said}" if said else "")
    body = "\n".join(lines)
    return f"<details{' open' if unfold else ''}><summary>{title}</summary>\n\n{body}\n\n</details>"


def step_mark(step: Step, before: dict[str, Step], diff: bool) -> str:
    """new, changed or same: a step against the base revision's steps of the same command."""
    if not diff:  # nothing to compare with
        return "same"
    old = before.get(step.label)
    if old is None:  # not on this command's path before
        return "new"
    return "changed" if old.text != step.text else "same"


def visible(steps: list[Step], marks: dict[str, str], *, full: bool) -> set[str]:
    """The steps worth a line: the handler, changed steps, steps that touch the outside or, shown in
    full, refuse something, and every caller above them."""
    callers = {s.label: s.caller for s in steps}
    refuses = {s.label for s in steps if any(rule.startswith("✋") for rule in s.rules)}
    keep = {
        s.label
        for s in steps
        if not s.depth or marks[s.label] != "same" or s.effects or (full and s.label in refuses)
    }
    for label in list(keep):
        caller = callers.get(label)
        while caller and caller not in keep:  # a caller with no line yet
            keep.add(caller)
            caller = callers.get(caller)
    return keep


def step_lines(step: Step, old: Step | None, mark: str, compare: bool, *, rules_too: bool) -> list[str]:
    """A step's line, then its rules when it changed or the whole app is shown; new ones marked."""
    pad = "  " * step.depth
    known = set(old.effects) if old and compare else set(step.effects)
    touches = ", ".join(
        ("➕ " if e not in known else "") + ("🟡 " if not e.named else "") + f"{e.verb} `{e.target}`"
        for e in step.effects
    )
    doc = f"*{escape(step.doc)}*" if mark != "same" and step.doc else ""
    bits = [f"`{step.label}`", f"`{step.shape}`" if step.shape else "", touches, doc]
    line = f"{pad}- {MARK[mark]}" + " · ".join(bit for bit in bits if bit)
    if not rules_too:  # an unchanged step's rules stay out of a branch's view
        return [line]
    was = set(old.rules) if old else set(step.rules)
    lines = [line, *(f"{pad}  - {'➕ ' if r not in was else ''}{escape(r)}" for r in step.rules)]
    return lines + [f"{pad}  - ➖ ~~{escape(r)}~~" for r in (old.rules if old else []) if r not in step.rules]


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
    args = [*positional(label, node), *node.args.kwonlyargs]
    notes = [a.annotation for a in args if a.annotation] + ([node.returns] if node.returns else [])
    return found + [Gap(label, f"passes `{annotation(a)}`, which names no fields") for a in notes if plain(a)]


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


def changed(base_root: Path, head_root: Path, folders: Iterable[str] = ("src", "scripts")) -> tuple[str, int]:
    """What a branch changed in the app, as markdown, and how many things new or edited code leaves unnamed."""
    head, base = build(head_root), build(base_root)
    found = [g for folder in folders for g in folder_gaps(base_root, head_root, folder)]
    found = list(dict.fromkeys(found + step_gaps(head, base)))
    if head is None:  # no app to show
        return gaps_section(found), len(found)
    # read top down and cut from the bottom: the data folds go first, the map and the gaps never
    shown = [stores_section(head, base), commands_section(head, base), types_section(head, base)]
    note = "" if any(shown) else f"This branch changes nothing on `{head.script}`'s path."
    sections = [f"### How `{head.script}` works, and what this branch changes", map_table(head, base), note]
    return "\n\n".join(s for s in [*sections, gaps_section(found), *shown] if s), len(found)


def whole(root: Path) -> str:
    """The whole app: the map, its files and services, every command's steps with their rules, its data."""
    app = build(root)
    if app is None:  # no console script
        return "No console script in pyproject.toml."
    shown = [
        map_table(app, None),
        stores_section(app, None),
        commands_section(app, None),
        types_section(app, None),
    ]
    return "\n\n".join(s for s in shown if s)


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
    """Print the whole app, or with `--changed` what a branch changed in it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", metavar="BASE", help="only what changed since this revision")
    args = parser.parse_args(argv)
    if not args.changed:  # the whole app
        print(whole(Path.cwd()))
        return 0
    with tempfile.TemporaryDirectory() as folder:
        print(changed(checkout(args.changed, Path(folder)), Path.cwd())[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
