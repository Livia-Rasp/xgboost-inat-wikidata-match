"""Baseline behaviour and the metric functions every reported number goes through.

No network: `build_observation_counts` is monkeypatched, since the real one calls the iNat API.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import evaluate
from src.evaluate import baseline_predict, ranking_score_column, review_queue_reduction, score_baseline
from src.train import (
    brier_score,
    find_auto_accept_threshold,
    find_reject_threshold,
    precision_at_threshold_table,
    top1_accuracy_and_mrr,
)


@pytest.fixture
def no_network(monkeypatch):
    """Observation counts, without the API. Prunella the mint (52765) is the more-observed of
    the two, matching the real numbers the milestone 5 tiebreak resolves."""
    counts = pd.DataFrame([
        {"taxon_id": "13982", "observations_count": 55702},
        {"taxon_id": "52765", "observations_count": 222630},
    ])
    monkeypatch.setattr(evaluate, "build_observation_counts", lambda ids, **kw: counts)


def _exact_pair() -> pd.DataFrame:
    return pd.DataFrame([
        {"wikidata_qid": "Q1", "inat_taxon_id": "13982", "strategy_exact": True, "label": 0,
         "fold": 0, "no_answer_reason": "has_positive"},
        {"wikidata_qid": "Q1", "inat_taxon_id": "52765", "strategy_exact": True, "label": 1,
         "fold": 0, "no_answer_reason": "has_positive"},
    ])


def test_baseline_breaks_a_tie_by_observation_count(no_network):
    predictions = baseline_predict(_exact_pair())
    assert predictions.loc[predictions["wikidata_qid"] == "Q1", "predicted_taxon_id"].item() == "52765"


def test_baseline_abstains_when_no_exact_match_exists(no_network):
    features = _exact_pair()
    features["strategy_exact"] = False
    assert baseline_predict(features).empty


def test_baseline_scoring_counts_abstentions_by_reason(no_network):
    features = pd.concat([
        _exact_pair(),
        # An item with no correct answer at all and no exact match: abstaining is right.
        pd.DataFrame([{
            "wikidata_qid": "Q2", "inat_taxon_id": "999", "strategy_exact": False, "label": 0,
            "fold": 0, "no_answer_reason": "stale_p3151",
        }]),
    ], ignore_index=True)

    fold_scores, abstention = score_baseline(features, baseline_predict(features))

    assert fold_scores.loc["overall", "n_items"] == 2
    assert fold_scores.loc["overall", "coverage"] == pytest.approx(0.5)
    assert fold_scores.loc["overall", "precision"] == pytest.approx(1.0)
    assert fold_scores.loc["overall", "accuracy"] == pytest.approx(1.0)
    assert abstention.loc["stale_p3151", "abstention_accuracy"] == pytest.approx(1.0)


def test_top1_and_mrr_reward_ranking_the_true_row_first():
    df = pd.DataFrame([
        {"wikidata_qid": "Q1", "label": 1, "score": 0.9},
        {"wikidata_qid": "Q1", "label": 0, "score": 0.4},
        {"wikidata_qid": "Q2", "label": 0, "score": 0.8},
        {"wikidata_qid": "Q2", "label": 0, "score": 0.7},
        {"wikidata_qid": "Q2", "label": 1, "score": 0.6},
    ])
    top1, mrr = top1_accuracy_and_mrr(df, "score")
    assert top1 == pytest.approx(0.5)          # Q1 right, Q2 wrong
    assert mrr == pytest.approx((1.0 + 1 / 3) / 2)  # Q2's true row is third


def test_the_observation_count_fixture_covers_every_gold_tie(no_network):
    """`make gold` is only offline while this holds.

    score_gold_set -> baseline_predict -> build_observation_counts asks for the taxon ids in a
    gold exact-match tie. If the fixture stops covering them, _observation_counts_fixture()
    correctly declines to zero-fill (0 is a real tie-break value) and the call goes to
    api.inaturalist.org — so the five-minute path quietly needs the network again and the
    committed baseline number starts depending on live counts that drift.
    """
    from src.evaluate import _observation_counts_fixture, load_gold_features

    # strategy_exact off the built frame, not re-derived from the `strategies` string. Deriving
    # it with a substring test is the bug milestone 15 fixed, and it reappeared here and in
    # build_fixtures.py — three places, all of which then disagreed with what baseline_predict()
    # actually asks for.
    features = load_gold_features()
    exact = features[features["strategy_exact"]]
    sizes = exact.groupby("wikidata_qid")["inat_taxon_id"].transform("size")
    tied = sorted(set(exact.loc[sizes > 1, "inat_taxon_id"]))

    assert tied, "no exact-match ties in the gold set — this test would prove nothing"
    covered = _observation_counts_fixture(tied)
    assert covered is not None, (
        f"the fixture does not cover all {len(tied)} tied taxon ids; "
        "rerun `python build_fixtures.py`"
    )
    assert set(covered["taxon_id"]) == set(tied)


def test_ranking_uses_the_raw_score_not_the_calibrated_one():
    """A calibrated probability is a step function and can flatten a whole group onto one value,
    which destroys within-group ordering. See evaluate.ranking_score_column()."""
    assert ranking_score_column("binary") == "binary_raw_score"
    assert ranking_score_column("rank") == "rank_raw_score"

    df = pd.DataFrame([
        {"wikidata_qid": "Q1", "label": 0, "raw": 0.99, "calibrated": 0.5},
        {"wikidata_qid": "Q1", "label": 1, "raw": 0.98, "calibrated": 0.5},
    ])
    # Tied on the calibrated column, so the ranking is decided by row order — the exact failure
    # the raw score avoids.
    assert top1_accuracy_and_mrr(df, "calibrated")[0] == 0.0
    assert top1_accuracy_and_mrr(df.iloc[::-1], "calibrated")[0] == 1.0
    # The raw column orders the same either way.
    assert top1_accuracy_and_mrr(df, "raw")[0] == top1_accuracy_and_mrr(df.iloc[::-1], "raw")[0]


def test_precision_at_threshold_table_is_monotone_in_the_obvious_direction():
    probs = np.array([0.1, 0.2, 0.8, 0.9, 0.95])
    labels = np.array([0, 0, 1, 1, 1])
    table = precision_at_threshold_table(probs, labels, n_steps=11)

    assert table.loc[table["threshold"] == 0.0, "coverage"].item() == pytest.approx(1.0)
    assert table.loc[table["threshold"] == 0.8, "precision"].item() == pytest.approx(1.0)
    assert table["coverage"].is_monotonic_decreasing


def test_auto_accept_threshold_is_the_lowest_one_that_clears_the_bar():
    probs = np.array([0.1, 0.5, 0.8, 0.9, 0.95])
    labels = np.array([0, 0, 1, 1, 1])
    row = find_auto_accept_threshold(precision_at_threshold_table(probs, labels), target_precision=1.0)
    assert row is not None
    # The sweep is a 0.01 grid, so the answer is the first step above the 0.5-scored negative,
    # not the first step above the next positive.
    assert row["threshold"] == pytest.approx(0.51)
    assert row["precision"] == pytest.approx(1.0)
    assert row["n"] == 3


def test_no_threshold_clears_an_impossible_bar():
    probs = np.array([0.9, 0.9])
    labels = np.array([0, 1])
    assert find_auto_accept_threshold(precision_at_threshold_table(probs, labels)) is None


def test_reject_threshold_finds_the_highest_safe_cut():
    probs = np.array([0.01, 0.02, 0.03, 0.9])
    labels = np.array([0, 0, 0, 1])
    assert find_reject_threshold(probs, labels, target_neg_precision=1.0) == pytest.approx(0.9)


def test_brier_score_rewards_calibration():
    labels = np.array([1, 0])
    assert brier_score(np.array([1.0, 0.0]), labels) == pytest.approx(0.0)
    assert brier_score(np.array([0.5, 0.5]), labels) == pytest.approx(0.25)


def test_review_queue_reduction_is_measured_per_item():
    """Two items, four candidate rows. Q1's best candidate clears the accept threshold; Q2's
    whole group sits below the reject threshold, and it does have a true match — so it counts
    against `items_losing_true_match` rather than as a free saving."""
    features = pd.DataFrame([
        {"wikidata_qid": "Q1", "label": 1, "binary_raw_score": 5.0, "binary_calibrated_prob": 0.99},
        {"wikidata_qid": "Q1", "label": 0, "binary_raw_score": 1.0, "binary_calibrated_prob": 0.10},
        {"wikidata_qid": "Q2", "label": 1, "binary_raw_score": 0.5, "binary_calibrated_prob": 0.30},
        {"wikidata_qid": "Q2", "label": 0, "binary_raw_score": 0.2, "binary_calibrated_prob": 0.20},
    ])
    reference = {"thresholds": {"binary": {"accept": 0.9, "reject": 0.5}}}

    result = review_queue_reduction(features, reference, "binary")
    assert result["n_items"] == 2
    assert result["auto_accept_items"] == 1
    assert result["auto_accept_precision"] == pytest.approx(1.0)
    assert result["auto_reject_items"] == 1
    assert result["auto_reject_precision"] == pytest.approx(0.0)  # Q2 does have a true match
    assert result["items_losing_true_match"] == 1
    assert result["review_items"] == 0
    assert result["candidate_rows_ruled_out"] == pytest.approx(0.75)
