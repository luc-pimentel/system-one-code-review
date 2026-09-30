"""Scores for a model that answers with probabilities.

Three questions matter for a yes/no probability p: does p rank the yes cases above the no cases (AUROC,
average precision), does p mean what it says (Brier score, calibration error), and how much can be left
to the model if it only acts when it is sure (risk against coverage)?
"""

from collections.abc import Callable

import numpy as np


def ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks, ties sharing their average rank."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranked = np.empty(len(values))
    start = 0
    while start < len(values):
        end = start
        while end + 1 < len(values) and sorted_values[end + 1] == sorted_values[start]:
            end += 1
        ranked[order[start : end + 1]] = (start + end) / 2 + 1
        start = end + 1
    return ranked


def auroc(y: np.ndarray, p: np.ndarray) -> float:
    """The chance that a random yes case scores above a random no case (ties count half)."""
    y = np.asarray(y, dtype=bool)
    positives, negatives = int(y.sum()), int((~y).sum())
    if not positives or not negatives:
        return float("nan")
    return float(
        (ranks(np.asarray(p, dtype=float))[y].sum() - positives * (positives + 1) / 2)
        / (positives * negatives)
    )


def average_precision(y: np.ndarray, p: np.ndarray) -> float:
    """Precision averaged over the rank of every yes case, from the most to the least likely."""
    y = np.asarray(y, dtype=bool)[np.argsort(-np.asarray(p, dtype=float), kind="mergesort")]
    if not y.any():
        return float("nan")
    precision = np.cumsum(y) / np.arange(1, len(y) + 1)
    return float(precision[y].mean())


def brier(y: np.ndarray, p: np.ndarray) -> float:
    """Mean squared distance between the probability and what happened (0 is perfect)."""
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2))


def calibration(
    y: np.ndarray, p: np.ndarray, bins: int = 10
) -> tuple[float, list[tuple[float, float, int, float, float]]]:
    """Expected calibration error over equal-width bins of p, and the bins: (low, high, count, mean p,
    share that were yes). A calibrated model's cases at 0.8 are yes 80% of the time."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    table = []
    error = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        inside = (p >= low) & ((p < high) if b < bins - 1 else (p <= high))
        if inside.any():
            mean_p, rate = float(p[inside].mean()), float(y[inside].mean())
            table.append((low, high, int(inside.sum()), mean_p, rate))
            error += inside.sum() / len(p) * abs(mean_p - rate)
    return float(error), table


def risk_coverage(
    y: np.ndarray, p: np.ndarray, threshold: float = 0.5
) -> tuple[float, np.ndarray, np.ndarray]:
    """Answer yes when p >= threshold, and take the answers the model is surest of first (p farthest from
    the threshold's side). For each share of cases taken (coverage), the error rate among them (risk).
    The area under the curve (AURC) is its mean: lower is better."""
    y, p = np.asarray(y, dtype=bool), np.asarray(p, dtype=float)
    answer = p >= threshold
    sureness = np.where(answer, p, 1 - p)
    order = np.argsort(-sureness, kind="mergesort")
    wrong = (answer != y)[order]
    taken = np.arange(1, len(y) + 1)
    risk = np.cumsum(wrong) / taken
    return float(risk.mean()), taken / len(y), risk


def coverage_at(coverage: np.ndarray, risk: np.ndarray, max_risk: float) -> float:
    """The largest share of cases whose error rate stays within `max_risk`."""
    within = np.nonzero(risk <= max_risk)[0]
    return float(coverage[within.max()]) if len(within) else 0.0


def bootstrap(
    statistic: Callable[..., float], *arrays: np.ndarray, samples: int = 1000, seed: int = 0
) -> tuple[float, float]:
    """A 95% interval for a statistic, from resampling the cases with replacement."""
    rng = np.random.default_rng(seed)
    n = len(arrays[0])
    values = []
    for _ in range(samples):
        pick = rng.integers(0, n, n)
        value = statistic(*(np.asarray(a)[pick] for a in arrays))
        if not np.isnan(value):
            values.append(value)
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def accuracy(truth: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.asarray(truth) == np.asarray(predicted)))


def macro_f1(truth: list[str], predicted: list[str]) -> float:
    """F1 averaged over the classes that occur in the truth, each class counting the same."""
    scores = []
    for label in sorted(set(truth)):
        tp = sum(t == label and p == label for t, p in zip(truth, predicted, strict=True))
        fp = sum(t != label and p == label for t, p in zip(truth, predicted, strict=True))
        fn = sum(t == label and p != label for t, p in zip(truth, predicted, strict=True))
        scores.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    return float(np.mean(scores))
