import ast

from scripts import lineage, mermaid


def test_labels_keep_line_breaks_and_escape_the_rest():
    assert mermaid.label('<run>/a "b"<br/><i>Row</i>') == "#lt;run#gt;/a #quot;b#quot;<br/><i>Row</i>"
    assert mermaid.ident("data/rows.jsonl") == "data_rows_jsonl"


def test_a_compared_chart_outlines_what_moved():
    before = mermaid.Chart(
        [mermaid.Node("a", "A", "step"), mermaid.Node("b", "B", "file")], [mermaid.Edge("a", "b")]
    )
    now = mermaid.Chart(
        [mermaid.Node("a", "A", "step"), mermaid.Node("c", "C", "file")], [mermaid.Edge("a", "c")]
    )
    chart = mermaid.compare(now, before)
    assert chart.marked()
    text = mermaid.render(chart)
    assert '  c[("C")]\n  b[("B")]\n  a --> c\n  a --> b\n' in text
    assert "  class c new\n  class b removed\n" in text
    assert "  linkStyle 0 stroke:#2da44e,stroke-width:3px\n  linkStyle 1 stroke:#cf222e" in text
    assert not mermaid.compare(before, before).marked()


def test_data_is_followed_back_through_names_calls_and_filled_containers():
    source = """
def build(items: list[Item]) -> Report:
    totals: Totals = load()
    rows = []
    for item in items:
        rows.append(score(item))
    return Report(rows=rows, totals=totals)
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    scope = lineage.Scope({"Item", "Report", "Score", "Totals"}, {"m.score": ("Score",), "m.load": ()})

    def resolve(expr: ast.expr) -> list[str]:
        return [f"m.{expr.id}"] if isinstance(expr, ast.Name) and expr.id in ("score", "load") else []

    found = lineage.transform("m.build", node, resolve, scope, None)
    assert found.takes == ("Item",)
    assert found.gives == ("Report",)
    assert set(found.feeds) == {"Score", "Item", "Totals"}  # appended, iterated, annotated
    assert found.typed == (lineage.Typed("m.load", "Totals"),)  # an untyped call, named where it is kept
