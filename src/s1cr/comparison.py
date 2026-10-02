"""Compare saved executions using one evaluator and explicitly matched PRs."""

import math
import statistics
from functools import partial
from pathlib import Path

import numpy as np

from . import metrics, provenance
from .score import PRICE_PER_MILLION_INPUT, category_cases, file_cases, file_correct, noul
from .swrbench import Row


def load_run(path: Path) -> tuple[dict, dict[str, dict]]:
    receipt = provenance.read(path)
    entries = provenance.records(path, receipt)
    answered = {entry["id"]: entry for entry in entries if "answers" in entry}
    failed = {entry["id"] for entry in entries if "error" in entry} - answered.keys()
    summary = {
        "path": str(path),
        "git_commit": receipt["git_commit"],
        "model": receipt["model"],
        "workers": receipt["workers"],
        "completed": len(answered),
        "failed": len(failed),
        "pending": len(receipt["eligible_ids"]) - len(answered) - len(failed),
    }
    return summary, answered


def compare(all_rows: list[Row], baseline_dir: Path, candidate_dir: Path) -> dict:
    baseline_receipt, candidate_receipt = provenance.read(baseline_dir), provenance.read(candidate_dir)
    if (
        baseline_receipt["dataset"] != candidate_receipt["dataset"]
        or set(baseline_receipt["case_ids"]) != set(candidate_receipt["case_ids"])
        or set(baseline_receipt["eligible_ids"]) != set(candidate_receipt["eligible_ids"])
    ):
        raise ValueError("runs must use the same benchmark inputs, labels, and selected cases")
    selected = provenance.select_rows(baseline_receipt, all_rows)
    provenance.select_rows(candidate_receipt, all_rows)
    baseline, left = load_run(baseline_dir)
    candidate, right = load_run(candidate_dir)
    # Canonical order also makes tied-score metrics independent of answer arrival order.
    rows = sorted((row for row in selected if row.id in left and row.id in right), key=lambda row: row.id)
    results = {}

    def measure(name, cases, statistic):
        values = [float(statistic(cases, answers)) for answers in (left, right)] if cases else [math.nan] * 2
        a, b = [value if math.isfinite(value) else None for value in values]
        results[name] = {
            "n": len(cases),
            "baseline": a,
            "candidate": b,
            "delta": b - a if a is not None and b is not None else None,
        }

    def binary(cases, answers, key, truth, statistic):
        values = np.array([noul(answers[row.id], key) for row in cases])
        if not np.all(np.isfinite(values) & (values >= 0) & (values <= 1)):
            raise ValueError(f"invalid probability in {key} answers")
        return statistic(np.array([truth(row) for row in cases]), values)

    for name, key, truth, statistic in (
        ("functional_auroc", "functional_defect", lambda row: row.functional, metrics.auroc),
        (
            "functional_accuracy",
            "functional_defect",
            lambda row: row.functional,
            lambda y, p: metrics.accuracy(y, p >= 0.5),
        ),
        ("functional_brier", "functional_defect", lambda row: row.functional, metrics.brier),
        ("changes_auroc", "changes_requested", lambda row: row.changes_requested, metrics.auroc),
    ):
        measure(name, rows, partial(binary, key=key, truth=truth, statistic=statistic))
    measure(
        "category_accuracy",
        category_cases(rows),
        lambda cases, answers: np.mean(
            [
                answers[row.id]["answers"]["problem_type"]["choice"] == row.categories[0].replace(".", "")
                for row in cases
            ]
        ),
    )
    measure(
        "file_accuracy",
        file_cases(rows),
        lambda cases, answers: np.mean([file_correct(row, answers[row.id]) for row in cases]),
    )
    measure(
        "median_ms", rows, lambda cases, answers: statistics.median(answers[row.id]["ms"] for row in cases)
    )

    def tokens(cases, answers, key):
        values = [(answers[row.id].get("usage") or {}).get(key) for row in cases]
        return sum(values) if all(isinstance(value, int) and value >= 0 for value in values) else math.nan

    measure("input_tokens", rows, lambda cases, answers: tokens(cases, answers, "input_tokens"))
    measure("output_tokens", rows, lambda cases, answers: tokens(cases, answers, "output_tokens"))
    measure(
        "estimated_input_usd",
        rows,
        lambda cases, answers: tokens(cases, answers, "input_tokens") / 1e6 * PRICE_PER_MILLION_INPUT,
    )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "evaluator": provenance.git_snapshot(),
        "cases": {
            "selected": len(selected),
            "eligible": len(baseline_receipt["eligible_ids"]),
            "excluded": len(selected) - len(baseline_receipt["eligible_ids"]),
            "matched": len(rows),
            "matched_ids": [row.id for row in rows],
            "baseline_only": sorted(left.keys() - right.keys()),
            "candidate_only": sorted(right.keys() - left.keys()),
        },
        "metrics": results,
        "input_price_per_million_usd": PRICE_PER_MILLION_INPUT,
    }


def readable(result: dict) -> str:
    left, right, cases = result["baseline"], result["candidate"], result["cases"]
    evaluator = result["evaluator"]
    lines = [
        f"Baseline:  {left['path']} @ {left['git_commit'][:12]} ({left['model']}, {left['workers']} workers)",
        f"Candidate: {right['path']} @ {right['git_commit'][:12]} ({right['model']}, {right['workers']} workers)",
        f"Evaluator: {evaluator['commit'][:12]}{' (uncommitted changes)' if evaluator['dirty'] else ''}",
        f"Cases: {cases['selected']} selected, {cases['excluded']} pre-excluded, {cases['eligible']} eligible",
        f"Completed: {left['completed']}/{cases['eligible']} baseline, {right['completed']}/{cases['eligible']} candidate",
        f"Failed/pending: {left['failed']}/{left['pending']} baseline, {right['failed']}/{right['pending']} candidate",
        f"All metrics below use the {cases['matched']} PRs answered by both runs (or the stated eligible subset).",
        "",
        "| Metric | PRs | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|---:|",
    ]
    labels = {
        "functional_auroc": "Functional-defect AUROC ↑",
        "functional_accuracy": "Functional-defect accuracy ↑",
        "functional_brier": "Functional-defect Brier ↓",
        "changes_auroc": "Changes-requested AUROC ↑",
        "category_accuracy": "Category accuracy ↑",
        "file_accuracy": "File accuracy ↑",
        "median_ms": "Median latency (ms) ↓",
        "input_tokens": "Input tokens ↓",
        "output_tokens": "Output tokens ↓",
        "estimated_input_usd": "Estimated input cost (USD) ↓",
    }

    def fmt(value, decimals, delta=False):
        return "n/a" if value is None else format(value, f"{'+' if delta else ''}.{decimals}f")

    for key, metric in result["metrics"].items():
        decimals = {"estimated_input_usd": 6, "median_ms": 1, "input_tokens": 0, "output_tokens": 0}.get(
            key, 3
        )
        lines.append(
            f"| {labels[key]} | {metric['n']} | {fmt(metric['baseline'], decimals)} | "
            f"{fmt(metric['candidate'], decimals)} | {fmt(metric['delta'], decimals, True)} |"
        )
    lines.extend(
        [
            "",
            "Delta = candidate − baseline. ↑ higher is better; ↓ lower is better.",
            f"Input cost uses ${result['input_price_per_million_usd']}/million tokens on matched successful answers; "
            "output tokens and failed/retried calls are not priced. n/a means insufficient cases or missing usage.",
        ]
    )
    return "\n".join(lines)
