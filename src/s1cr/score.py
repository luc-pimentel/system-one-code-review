"""Scoring Jev's answers against what SWR-Bench's reviewers found.

The headline numbers come from one run, since that is what a single call gets you; the other runs only
measure how much the answers move when the same pull request is asked again.
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np

from . import jev, metrics, questions
from .swrbench import CATEGORIES, SOURCE_COMMIT, SOURCE_REPO, Row

PRICE_PER_MILLION_INPUT = 0.042  # USD, TypeSafe's quoted price for Jev input tokens


@dataclass
class Binary:
    """One yes/no question: Jev's probability of yes against the reviewers' label."""

    key: str
    label: str  # what counts as yes in the data
    n: int
    positives: int
    auroc: float
    auroc_ci: tuple[float, float]
    average_precision: float
    brier: float
    brier_ci: tuple[float, float]
    brier_prevalence: float  # Brier score of always answering the share of yes cases
    ece: float
    bins: list[tuple[float, float, int, float, float]]
    accuracy: float
    accuracy_ci: tuple[float, float]
    majority: float  # accuracy of always giving the more common answer
    aurc: float
    aurc_random: float  # AURC of answers in random order: the error rate at every coverage
    coverage_5: float
    coverage_10: float
    size_auroc: float  # the diff's changed lines used as the score
    mean_yes: float  # mean probability on yes cases
    mean_no: float
    by_era: dict[str, tuple[int, float]] = field(default_factory=dict)
    by_repo: dict[str, tuple[int, float]] = field(default_factory=dict)


def binary(key: str, label: str, rows: list[Row], y: list[bool], p: list[float]) -> Binary:
    y_, p_ = np.array(y, dtype=bool), np.array(p, dtype=float)
    size = np.array([r.changed_lines for r in rows], dtype=float)
    prevalence = float(y_.mean())
    ece, bins = metrics.calibration(y_, p_)
    aurc, coverage, risk = metrics.risk_coverage(y_, p_)
    answer = p_ >= 0.5

    def grouped(names: list[str]) -> dict[str, tuple[int, float]]:
        out = {}
        for name in sorted(set(names)):
            pick = np.array([n == name for n in names])
            out[name] = (int(pick.sum()), metrics.auroc(y_[pick], p_[pick]))
        return out

    return Binary(
        key=key,
        label=label,
        n=len(y_),
        positives=int(y_.sum()),
        auroc=metrics.auroc(y_, p_),
        auroc_ci=metrics.bootstrap(metrics.auroc, y_, p_),
        average_precision=metrics.average_precision(y_, p_),
        brier=metrics.brier(y_, p_),
        brier_ci=metrics.bootstrap(metrics.brier, y_, p_),
        brier_prevalence=metrics.brier(y_, np.full(len(y_), prevalence)),
        ece=ece,
        bins=bins,
        accuracy=metrics.accuracy(y_, answer),
        accuracy_ci=metrics.bootstrap(lambda a, b: metrics.accuracy(a, b >= 0.5), y_, p_),
        majority=max(prevalence, 1 - prevalence),
        aurc=aurc,
        aurc_random=float(np.mean(answer != y_)),
        coverage_5=metrics.coverage_at(coverage, risk, 0.05),
        coverage_10=metrics.coverage_at(coverage, risk, 0.10),
        size_auroc=metrics.auroc(y_, size),
        mean_yes=float(p_[y_].mean()),
        mean_no=float(p_[~y_].mean()),
        by_era=grouped([r.era for r in rows]),
        by_repo=grouped([r.repo for r in rows]),
    )


@dataclass
class Choice:
    """One Choice question: Jev's top option against the reviewers' label."""

    key: str
    label: str
    n: int
    accuracy: float
    accuracy_ci: tuple[float, float]
    baselines: dict[str, float]
    confident: tuple[float, float]  # accuracy on the more and the less confident half
    extra: dict[str, float] = field(default_factory=dict)
    confusion: list[tuple[str, str, int]] = field(default_factory=list)  # most common mistakes


@dataclass
class Stability:
    runs: list[str]
    n: int  # pull requests answered in every run
    mean_change: dict[str, float]  # per yes/no question: mean |difference| between two runs
    spread_95: dict[str, float]  # per yes/no question: 95th percentile of max minus min across runs
    flips: dict[str, float]  # per yes/no question: share whose answer at 0.5 is not the same in every run
    agreement: dict[str, float]  # per Choice question: share with the same top option in every run


@dataclass
class Cost:
    calls: int
    input_tokens: int
    output_tokens: int
    median_input: float
    ms_median: float
    ms_90: float
    models: dict[str, int]

    @property
    def dollars(self) -> float:
        return self.input_tokens / 1e6 * PRICE_PER_MILLION_INPUT


@dataclass
class Results:
    source: str
    model: str
    primary: str
    total: int
    kept: int
    excluded: dict[str, int]
    eras: dict[str, int]
    changes: Binary
    functional: Binary
    functional_vs_clean: Binary
    problem_type: Choice
    fault_file: Choice
    stability: Stability | None
    cost: Cost


def noul(answer: dict, key: str) -> float:
    return float(answer["answers"][key]["noul"])


def halves(right: np.ndarray, confidence: np.ndarray) -> tuple[float, float]:
    """Accuracy on the more confident half of the answers, then on the less confident half."""
    order = np.argsort(-confidence, kind="mergesort")
    top = (len(order) + 1) // 2
    return float(right[order[:top]].mean()), float(right[order[top:]].mean()) if len(order) > 1 else float(
        "nan"
    )


def problem_type(rows: list[Row], answers: dict[str, dict]) -> Choice:
    """For pull requests with exactly one problem: is Jev's most likely kind of change that problem's?"""
    single = [r for r in rows if len(r.categories) == 1]
    truth = [r.categories[0].replace(".", "") for r in single]
    picked = [answers[r.id]["answers"]["problem_type"]["choice"] for r in single]
    confidence = np.array([answers[r.id]["answers"]["problem_type"]["confidence"] for r in single])
    right = np.array([t == p for t, p in zip(truth, picked, strict=True)])
    most_common, count = Counter(truth).most_common(1)[0]
    mistakes = Counter((t, p) for t, p in zip(truth, picked, strict=True) if t != p)
    return Choice(
        key="problem_type",
        label="the category of the one problem reviewers found",
        n=len(single),
        accuracy=float(right.mean()),
        accuracy_ci=metrics.bootstrap(lambda r: float(np.mean(r)), right),
        baselines={
            f"always {questions.OPTIONS[most_common]} {CATEGORIES[questions.OPTIONS[most_common]][0]}": count
            / len(single),
            f"random among {len(questions.OPTIONS)}": 1 / len(questions.OPTIONS),
        },
        confident=halves(right, confidence),
        extra={
            "functional or evolvability right": float(
                np.mean([t[0] == p[0] for t, p in zip(truth, picked, strict=True)])
            ),
            "macro F1": metrics.macro_f1(truth, picked),
        },
        confusion=[(questions.OPTIONS[t], questions.OPTIONS[p], n) for (t, p), n in mistakes.most_common(5)],
    )


def fault_file(rows: list[Row], answers: dict[str, dict]) -> Choice:
    """For pull requests reviewers asked to change that touch several files, with the problem's file known:
    does Jev's most likely file hold a problem?"""
    cases = [r for r in rows if r.changes_requested and len(r.files) > 1 and r.fault_files]
    right, confidence, largest = [], [], []
    for row in cases:
        answer = answers[row.id]["answers"]["fault_file"]
        right.append(row.files[int(answer["choice"][1:])].path in row.fault_files)
        confidence.append(answer["confidence"])
        largest.append(max(row.files, key=lambda f: f.changed).path in row.fault_files)
    right_, confidence_ = np.array(right), np.array(confidence)
    return Choice(
        key="fault_file",
        label="a file holding a problem reviewers found",
        n=len(cases),
        accuracy=float(right_.mean()),
        accuracy_ci=metrics.bootstrap(lambda r: float(np.mean(r)), right_),
        baselines={
            "the file with the most changed lines": float(np.mean(largest)),
            "a random file": float(np.mean([len(r.fault_files) / len(r.files) for r in cases])),
        },
        confident=halves(right_, confidence_),
        extra={"median files per pull request": float(statistics.median(len(r.files) for r in cases))},
    )


def stability(rows: list[Row], runs: dict[str, dict[str, dict]]) -> Stability | None:
    if len(runs) < 2:
        return None
    names = sorted(runs)
    ids = [r.id for r in rows if all(r.id in runs[name] for name in names)]
    out = Stability(names, len(ids), {}, {}, {}, {})
    for key in ("changes_requested", "functional_defect"):
        values = np.array([[noul(runs[name][i], key) for name in names] for i in ids])
        pairs = [abs(values[:, a] - values[:, b]) for a, b in combinations(range(len(names)), 2)]
        out.mean_change[key] = float(np.mean(pairs))
        out.spread_95[key] = float(np.percentile(values.max(axis=1) - values.min(axis=1), 95))
        answers = values >= 0.5
        out.flips[key] = float(np.mean(answers.any(axis=1) & ~answers.all(axis=1)))
    for key in ("problem_type", "fault_file"):
        asked = [i for i in ids if key in runs[names[0]][i]["answers"]]
        same = [len({runs[name][i]["answers"][key]["choice"] for name in names}) == 1 for i in asked]
        out.agreement[key] = float(np.mean(same))
    return out


def cost(answers: dict[str, dict]) -> Cost:
    results = list(answers.values())
    inputs = [r["usage"]["input_tokens"] for r in results]
    ms = [r["ms"] for r in results]
    return Cost(
        calls=len(results),
        input_tokens=sum(inputs),
        output_tokens=sum(r["usage"]["output_tokens"] for r in results),
        median_input=float(statistics.median(inputs)),
        ms_median=float(statistics.median(ms)),
        ms_90=float(np.percentile(ms, 90)),
        models=dict(Counter(r["model"] for r in results)),
    )


def score(all_rows: list[Row], runs_dir: Path, primary: str) -> Results:
    runs = {path.parent.name: jev.load(path) for path in sorted(runs_dir.glob("*/answers.jsonl"))}
    if primary not in runs:
        raise FileNotFoundError(f"no run named {primary} in {runs_dir}")
    answers = runs[primary]
    refused = jev.refused(runs_dir / primary / "answers.jsonl")
    for row in all_rows:
        if row.excluded is None and row.id in refused:
            row.excluded = "over Jev's token limit"
    kept = [r for r in all_rows if r.excluded is None]
    rows = [r for r in kept if r.id in answers]
    if len(rows) != len(kept):
        raise ValueError(
            f"run {primary} answered {len(rows)} of {len(kept)} pull requests; rerun it to finish"
        )
    excluded = Counter(r.excluded.split(":")[0] for r in all_rows if r.excluded)
    clean_or_functional = [r for r in rows if r.functional or not r.changes_requested]
    return Results(
        source=f"{SOURCE_REPO}@{SOURCE_COMMIT[:7]}",
        model=Counter(a["model"] for a in answers.values()).most_common(1)[0][0],
        primary=primary,
        total=len(all_rows),
        kept=len(rows),
        excluded=dict(excluded),
        eras=dict(Counter(r.era for r in rows)),
        changes=binary(
            "changes_requested",
            "reviewers asked for at least one change",
            rows,
            [r.changes_requested for r in rows],
            [noul(answers[r.id], "changes_requested") for r in rows],
        ),
        functional=binary(
            "functional_defect",
            "reviewers found at least one functional problem",
            rows,
            [r.functional for r in rows],
            [noul(answers[r.id], "functional_defect") for r in rows],
        ),
        functional_vs_clean=binary(
            "functional_defect",
            "functional problem, against pull requests approved as they were",
            clean_or_functional,
            [r.functional for r in clean_or_functional],
            [noul(answers[r.id], "functional_defect") for r in clean_or_functional],
        ),
        problem_type=problem_type(rows, answers),
        fault_file=fault_file(rows, answers),
        stability=stability(rows, runs),
        cost=cost(answers),
    )
