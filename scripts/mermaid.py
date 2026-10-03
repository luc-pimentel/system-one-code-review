"""Mermaid flowcharts as GitHub renders them: boxes in a shape, arrows with labels, and what a branch
added, changed or removed outlined in colour."""

import re
from dataclasses import dataclass, field, replace

SHAPES = {
    "step": '["{}"]',  # a rectangle: something the app does
    "file": '[("{}")]',  # a cylinder: a file the app keeps
    "service": '{{{{"{}"}}}}',  # a hexagon: a service the app calls
    "data": '("{}")',  # a rounded box: a data type
    "end": '(["{}"])',  # a stadium: where output leaves the app
}
ARROWS = {
    "flow": "-->",  # data moves one way
    "both": "<-->",  # read and written
    "call": "-.-",  # a service called
    "part": "-.->",  # one data type kept inside another
}
COLOURS = {"new": "#2da44e", "changed": "#bf8700", "removed": "#cf222e"}
STYLES = {
    "new": f"stroke:{COLOURS['new']},stroke-width:3px",
    "changed": f"stroke:{COLOURS['changed']},stroke-width:3px",
    "removed": f"stroke:{COLOURS['removed']},stroke-width:2px,stroke-dasharray:5 5",
}
LEGEND = "Outlined 🟩 new · 🟨 changed · 🟥 removed (dashed)"


@dataclass(frozen=True)
class Node:
    """One box in a flowchart."""

    id: str  # what arrows call it: letters, digits and underscores
    label: str  # what the box says; `<br/>` breaks a line, `<i>` sets a word apart
    shape: str  # step, file, service, data or end
    mark: str = "same"  # new, changed, removed or same: what a branch did to it


@dataclass(frozen=True)
class Edge:
    """One arrow between two boxes."""

    source: str  # the box it leaves
    target: str  # the box it points at
    label: str = ""  # what the arrow says
    kind: str = "flow"  # flow, both, call or part: how it is drawn
    mark: str = "same"  # new, removed or same: what a branch did to it


@dataclass
class Chart:
    """A flowchart: its boxes and arrows, drawn top down unless told otherwise."""

    nodes: list[Node] = field(default_factory=list)  # every box, in the order they are declared
    edges: list[Edge] = field(default_factory=list)  # every arrow, in the order they are drawn
    direction: str = "TD"  # TD draws top down, LR left to right

    def add(self, node: Node) -> None:
        """Declare a box once; a later declaration of the same id is ignored."""
        if all(known.id != node.id for known in self.nodes):  # not declared yet
            self.nodes.append(node)

    def marked(self) -> bool:
        """Whether a branch added, changed or removed anything drawn."""
        return any(n.mark != "same" for n in self.nodes) or any(e.mark != "same" for e in self.edges)


def ident(text: str) -> str:
    """Text as a Mermaid id: every character but letters, digits and underscores made an underscore."""
    return re.sub(r"\W", "_", text)


def label(text: str) -> str:
    """Text safe inside a quoted Mermaid label: quotes and angle brackets as Mermaid's entity codes, the
    `<br/>` and `<i>` tags it renders kept."""
    kept = {"<br/>": "\0b", "<i>": "\0i", "</i>": "\0e"}
    for tag, stand_in in kept.items():
        text = text.replace(tag, stand_in)
    text = text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")
    for tag, stand_in in kept.items():
        text = text.replace(stand_in, tag)
    return text


def arrow(edge: Edge) -> str:
    """One arrow as a line of Mermaid."""
    said = f'|"{label(edge.label)}"|' if edge.label else ""
    return f"  {edge.source} {ARROWS[edge.kind]}{said} {edge.target}"


def mark(item: Node | Edge, old: Node | Edge | None) -> str:
    """new, changed or same: a box or an arrow against the one it was before."""
    if old is None:  # not drawn before
        return "new"
    return "changed" if old.label != item.label else "same"


def compare(now: Chart, before: Chart) -> Chart:
    """The chart as it is now, with what was added or changed since `before` marked and what was removed
    drawn again, marked removed."""
    was_nodes = {n.id: n for n in before.nodes}
    was_edges = {(e.source, e.target, e.kind): e for e in before.edges}
    ids = {n.id for n in now.nodes}
    keys = {(e.source, e.target, e.kind) for e in now.edges}
    nodes = [replace(n, mark=mark(n, was_nodes.get(n.id))) for n in now.nodes]
    nodes += [replace(n, mark="removed") for n in before.nodes if n.id not in ids]
    edges = [replace(e, mark=mark(e, was_edges.get((e.source, e.target, e.kind)))) for e in now.edges]
    edges += [replace(e, mark="removed") for e in before.edges if (e.source, e.target, e.kind) not in keys]
    return Chart(nodes, edges, now.direction)


def render(chart: Chart) -> str:
    """The chart as a fenced Mermaid block, with what a branch touched outlined."""
    lines = ["```mermaid", f"flowchart {chart.direction}"]
    lines += [f"  {n.id}{SHAPES[n.shape].format(label(n.label))}" for n in chart.nodes]
    lines += [arrow(e) for e in chart.edges]
    used = [mark for mark in STYLES if any(n.mark == mark for n in chart.nodes)]
    lines += [f"  classDef {mark} {STYLES[mark]}" for mark in used]
    lines += [f"  class {','.join(n.id for n in chart.nodes if n.mark == m)} {m}" for m in used]
    for mark, style in STYLES.items():
        drawn = [str(i) for i, e in enumerate(chart.edges) if e.mark == mark]
        if drawn:  # some arrows carry this mark
            lines.append(f"  linkStyle {','.join(drawn)} {style}")
    return "\n".join([*lines, "```"])
