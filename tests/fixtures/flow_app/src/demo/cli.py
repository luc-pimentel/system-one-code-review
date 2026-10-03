"""Demo's commands."""

import argparse
from pathlib import Path

import httpx

from . import files, stores


def score(args: argparse.Namespace) -> None:
    """Score every item into a run."""
    items = files.load()
    if not items:  # nothing to score
        raise SystemExit("no items")
    with httpx.Client() as client:
        scores = [files.ask(client, item) for item in items]
    files.save(scores, stores.DATA / args.name)
    print(files.version(), files.token())


def show(args: argparse.Namespace) -> None:
    """Print one file."""
    print(files.stray(Path(args.path)))


def main(argv: list[str] | None = None) -> None:
    """Hand the subcommand to its function."""
    parser = argparse.ArgumentParser(prog="demo")
    commands = parser.add_subparsers(dest="command", required=True)
    score_cmd = commands.add_parser("score", help="score every item")
    score_cmd.add_argument("name")
    score_cmd.set_defaults(func=score)
    show_cmd = commands.add_parser("show", help="print a file")
    show_cmd.add_argument("path")
    show_cmd.set_defaults(func=show)
    args = parser.parse_args(argv)
    args.func(args)
