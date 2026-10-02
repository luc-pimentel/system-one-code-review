import numpy as np
import pytest

from s1cr import metrics


def test_ties_share_their_average_rank():
    assert metrics.ranks(np.array([1.0, 2.0, 2.0, 3.0])).tolist() == [1, 2.5, 2.5, 4]


def test_auroc_counts_the_pairs_ranked_the_right_way():
    y = np.array([0, 0, 1, 1])
    assert metrics.auroc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert metrics.auroc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert metrics.auroc(y, np.full(4, 0.5)) == 0.5
    # 0.35 beats 0.1 but not 0.4; 0.8 beats both: 3 of 4 pairs
    assert metrics.auroc(y, np.array([0.1, 0.4, 0.35, 0.8])) == 0.75
    assert np.isnan(metrics.auroc(np.ones(3), np.array([0.1, 0.2, 0.3])))


def test_average_precision_averages_precision_at_each_yes():
    assert metrics.average_precision(np.array([1, 0, 1]), np.array([0.9, 0.8, 0.7])) == pytest.approx(
        (1 + 2 / 3) / 2
    )


def test_brier_is_zero_when_sure_and_right():
    assert metrics.brier(np.array([1, 0]), np.array([1.0, 0.0])) == 0
    assert metrics.brier(np.array([1, 0]), np.array([0.5, 0.5])) == 0.25


def test_a_calibrated_bin_has_no_error():
    error, bins = metrics.calibration(np.array([1, 0, 0, 0]), np.full(4, 0.25))
    assert error == 0
    assert bins == [(0.2, 0.3, 4, 0.25, 0.25)]
    error, _ = metrics.calibration(np.array([0, 0]), np.array([0.9, 0.9]))
    assert error == pytest.approx(0.9)


def test_risk_coverage_takes_the_surest_answers_first():
    y = np.array([1, 1, 0, 0])
    aurc, coverage, risk = metrics.risk_coverage(y, np.array([0.9, 0.6, 0.4, 0.2]))
    assert aurc == 0
    assert metrics.coverage_at(coverage, risk, 0.05) == 1.0
    # 0.45 answers no on a yes case, and it is the least sure answer, so it comes last
    aurc, coverage, risk = metrics.risk_coverage(y, np.array([0.9, 0.45, 0.4, 0.2]))
    assert risk.tolist() == [0, 0, 0, 0.25]
    assert aurc == pytest.approx(0.0625)
    assert metrics.coverage_at(coverage, risk, 0.05) == 0.75


def test_bootstrap_brackets_a_stable_statistic():
    y = np.array([0, 1] * 50)
    p = np.where(y == 1, 0.8, 0.2)
    low, high = metrics.bootstrap(metrics.auroc, y, p, samples=200)
    assert low == high == 1.0


def test_macro_f1_weighs_each_class_the_same():
    assert metrics.macro_f1(["a", "a", "b"], ["a", "a", "b"]) == 1.0
    assert metrics.macro_f1(["a", "a", "b"], ["a", "a", "a"]) == pytest.approx((0.8 + 0) / 2)
