"""Where demo keeps its files, and the services it calls."""

from pathlib import Path

DATA = Path("data")  # everything demo keeps
ITEMS = DATA / "items.jsonl"  # one Item per line
API = "https://api.example.com"  # the example service: scores one item
TOOL = "tool"  # a program that says its version


def results(run: Path) -> Path:
    """One Score per line."""
    return run / "results.jsonl"
