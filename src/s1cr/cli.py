"""Review a GitHub PR, or run and score the SWR-Bench evaluation."""

import argparse
import json
import os
from pathlib import Path

import httpx

from . import github, jev, stores, swrbench
from .models import DEFAULT_MODEL, ReviewConfig
from .presentation import document, readable


def load_rows(path: Path = stores.ROWS) -> list[swrbench.Row]:
    """The benchmark rows `s1cr build` wrote."""
    if not path.exists():  # the rows were never built
        raise FileNotFoundError(f"{path} is missing; run `s1cr build` first")
    return [swrbench.Row.from_json(line) for line in path.read_text().splitlines() if line]


def api_key() -> str:
    """The TypeSafe API key from the environment."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:  # no API key in the environment
        raise SystemExit("error: TYPESAFE_API_KEY is not set")
    return key


def fetch(args: argparse.Namespace) -> None:
    """Download SWR-Bench at its pinned commit."""
    path = swrbench.download(stores.SOURCE)
    print(f"{path}: {len(swrbench.load(path))} pull requests")


def build(args: argparse.Namespace) -> None:
    """Turn every SWR-Bench record into a row a model reads, and say why the left-out ones are."""
    records = swrbench.load(swrbench.download(stores.SOURCE))
    comparer = github.Comparer(stores.COMPARE_CACHE, github.token())
    rows = [swrbench.to_row(record, comparer) for record in records]
    stores.ROWS.write_text("".join(row.to_json() + "\n" for row in rows))
    kept = [r for r in rows if r.excluded is None]
    print(f"{len(kept)} of {len(rows)} pull requests kept in {stores.ROWS}")
    reasons: dict[str, int] = {}
    for row in rows:
        if row.excluded:  # the row was left out of the benchmark
            key = row.excluded.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"  left out {count}: {reason}")


def run(args: argparse.Namespace) -> None:
    """Ask Jev every row's questions, recording the run so it can be resumed and compared."""
    run_dir = stores.RUNS / args.name
    try:
        if args.limit is not None and args.limit < 1:  # a limit below one row was asked for
            raise ValueError("limit must be at least 1")
        rows = load_rows()[: args.limit]
        ok, failed = jev.run(rows, run_dir, args.model, api_key(), args.workers)
    except (OSError, ValueError) as error:  # the rows or the run directory are unusable
        raise SystemExit(f"error: {error}") from error
    print(f"{ok} answered, {failed} failed, in {stores.answers(run_dir)}")
    if failed:  # some calls failed, so the run is incomplete
        raise SystemExit(1)


def score(args: argparse.Namespace) -> None:
    """Score every run against the reviewers' labels and write the report."""
    from .report import write_report
    from .score import score as score_runs

    write_report(score_runs(load_rows(), stores.RUNS, args.primary), stores.REPORT)
    print(f"wrote {stores.REPORT}")


def review(args: argparse.Namespace) -> None:
    """Assess one live GitHub pull request with Jev."""
    try:
        github.parse_pr_url(args.url)
        key = api_key()
        pr = github.pull_request(args.url)
        result = jev.review(pr.input, ReviewConfig(model=args.model), api_key=key)
    except (
        github.GitHubError,
        jev.JevError,
        httpx.HTTPError,
        ValueError,
    ) as error:  # GitHub, Jev, the network or the URL failed
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(document(pr, result), ensure_ascii=False) if args.json else readable(pr, result))


def compare(args: argparse.Namespace) -> None:
    """Compare two recorded runs on the pull requests both answered."""
    from .comparison import compare as compare_runs
    from .comparison import readable as readable_comparison

    def run_path(value: str) -> Path:
        path = Path(value)
        return stores.RUNS / path if len(path.parts) == 1 and not path.is_dir() else path

    try:
        result = compare_runs(load_rows(args.rows), run_path(args.baseline), run_path(args.candidate))
    except (OSError, ValueError) as error:  # a run directory or its receipt is unusable
        raise SystemExit(f"error: {error}") from error
    print(
        json.dumps(result, ensure_ascii=False, allow_nan=False) if args.json else readable_comparison(result)
    )


def main(argv: list[str] | None = None) -> None:
    """Read the subcommand and hand its arguments to the function that runs it."""
    parser = argparse.ArgumentParser(prog="s1cr", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = commands.add_parser("fetch", help="download SWR-Bench at the pinned commit into data/")
    fetch_cmd.set_defaults(func=fetch)
    build_cmd = commands.add_parser(
        "build", help="turn SWR-Bench into rows a model reads, into data/rows.jsonl"
    )
    build_cmd.set_defaults(func=build)
    run_cmd = commands.add_parser("run", help="ask Jev every row's questions, into runs/<name>/answers.jsonl")
    run_cmd.add_argument("name", help="run name, e.g. r1")
    run_cmd.add_argument("--model", required=True, help="pinned Jev version, e.g. jev-1.13.0")
    run_cmd.add_argument("--limit", type=int, help="only the first N rows, for a trial")
    run_cmd.add_argument("--workers", type=int, default=4)
    run_cmd.set_defaults(func=run)
    score_cmd = commands.add_parser("score", help="score every run and write reports/jev-swrbench.md")
    score_cmd.add_argument("--primary", default="r1", help="the run the headline numbers come from")
    score_cmd.set_defaults(func=score)
    compare_cmd = commands.add_parser("compare", help="compare two recorded runs on matched benchmark PRs")
    compare_cmd.add_argument("baseline", help="run name or path to its directory, including another worktree")
    compare_cmd.add_argument("candidate", help="run name or path to its directory")
    compare_cmd.add_argument(
        "--rows", type=Path, default=stores.ROWS, help="benchmark rows used by both runs"
    )
    compare_cmd.add_argument(
        "--json", action="store_true", help="print comparison and cohort details as JSON"
    )
    compare_cmd.set_defaults(func=compare)
    review_cmd = commands.add_parser("review", help="review the current diff of a GitHub pull request")
    review_cmd.add_argument("url", help="https://github.com/owner/repo/pull/123")
    review_cmd.add_argument("--model", default=DEFAULT_MODEL, help=f"Jev model (default {DEFAULT_MODEL})")
    review_cmd.add_argument(
        "--json", action="store_true", help="print raw answers, metadata, and option mappings as JSON"
    )
    review_cmd.set_defaults(func=review)
    args = parser.parse_args(argv)
    args.func(args)
