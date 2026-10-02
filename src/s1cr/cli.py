"""Review a GitHub PR, or run and score the SWR-Bench evaluation."""

import argparse
import json
import os
from pathlib import Path

import httpx

from . import github, jev, swrbench
from .models import DEFAULT_MODEL, ReviewConfig
from .presentation import document, readable

DATA = Path("data")
SOURCE = DATA / "swrbench" / "swr_datasets_d5c5.jsonl"
COMPARE_CACHE = DATA / "compare"
ROWS = DATA / "rows.jsonl"
RUNS = Path("runs")
REPORT = Path("reports/jev-swrbench.md")


def load_rows(path: Path = ROWS) -> list[swrbench.Row]:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run `s1cr build` first")
    return [swrbench.Row.from_json(line) for line in path.read_text().splitlines() if line]


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise SystemExit("error: TYPESAFE_API_KEY is not set")
    return key


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="s1cr", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="download SWR-Bench at the pinned commit into data/")
    commands.add_parser("build", help="turn SWR-Bench into rows a model reads, into data/rows.jsonl")
    run_cmd = commands.add_parser("run", help="ask Jev every row's questions, into runs/<name>/answers.jsonl")
    run_cmd.add_argument("name", help="run name, e.g. r1")
    run_cmd.add_argument("--model", required=True, help="pinned Jev version, e.g. jev-1.13.0")
    run_cmd.add_argument("--limit", type=int, help="only the first N rows, for a trial")
    run_cmd.add_argument("--workers", type=int, default=4)
    score_cmd = commands.add_parser("score", help="score every run and write reports/jev-swrbench.md")
    score_cmd.add_argument("--primary", default="r1", help="the run the headline numbers come from")
    compare_cmd = commands.add_parser("compare", help="compare two recorded runs on matched benchmark PRs")
    compare_cmd.add_argument("baseline", help="run name or path to its directory, including another worktree")
    compare_cmd.add_argument("candidate", help="run name or path to its directory")
    compare_cmd.add_argument("--rows", type=Path, default=ROWS, help="benchmark rows used by both runs")
    compare_cmd.add_argument(
        "--json", action="store_true", help="print comparison and cohort details as JSON"
    )
    review_cmd = commands.add_parser("review", help="review the current diff of a GitHub pull request")
    review_cmd.add_argument("url", help="https://github.com/owner/repo/pull/123")
    review_cmd.add_argument("--model", default=DEFAULT_MODEL, help=f"Jev model (default {DEFAULT_MODEL})")
    review_cmd.add_argument(
        "--json", action="store_true", help="print raw answers, metadata, and option mappings as JSON"
    )
    args = parser.parse_args(argv)

    if args.command == "fetch":
        path = swrbench.download(SOURCE)
        print(f"{path}: {len(swrbench.load(path))} pull requests")
    elif args.command == "build":
        records = swrbench.load(swrbench.download(SOURCE))
        comparer = github.Comparer(COMPARE_CACHE, github.token())
        rows = [swrbench.to_row(record, comparer) for record in records]
        ROWS.write_text("".join(row.to_json() + "\n" for row in rows))
        kept = [r for r in rows if r.excluded is None]
        print(f"{len(kept)} of {len(rows)} pull requests kept in {ROWS}")
        reasons: dict[str, int] = {}
        for row in rows:
            if row.excluded:
                key = row.excluded.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
            print(f"  left out {count}: {reason}")
    elif args.command == "run":
        try:
            if args.limit is not None and args.limit < 1:
                raise ValueError("limit must be at least 1")
            rows = load_rows()[: args.limit]
            ok, failed = jev.run(
                rows, RUNS / args.name / "answers.jsonl", args.model, api_key(), args.workers
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"error: {error}") from error
        print(f"{ok} answered, {failed} failed, in {RUNS / args.name}/answers.jsonl")
        if failed:
            raise SystemExit(1)
    elif args.command == "score":
        from .report import write_report
        from .score import score

        write_report(score(load_rows(), RUNS, args.primary), REPORT)
        print(f"wrote {REPORT}")
    elif args.command == "review":
        try:
            github.parse_pr_url(args.url)
            key = api_key()
            pr = github.pull_request(args.url)
            result = jev.review(pr.input, ReviewConfig(model=args.model), api_key=key)
        except (github.GitHubError, jev.JevError, httpx.HTTPError, ValueError) as error:
            raise SystemExit(f"error: {error}") from error
        print(json.dumps(document(pr, result), ensure_ascii=False) if args.json else readable(pr, result))
    elif args.command == "compare":
        from .comparison import compare
        from .comparison import readable as readable_comparison

        def run_path(value: str) -> Path:
            path = Path(value)
            return RUNS / path if len(path.parts) == 1 and not path.is_dir() else path

        try:
            result = compare(load_rows(args.rows), run_path(args.baseline), run_path(args.candidate))
        except (OSError, ValueError) as error:
            raise SystemExit(f"error: {error}") from error
        print(
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            if args.json
            else readable_comparison(result)
        )
