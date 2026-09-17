"""The promotion gate. Spec §7 milestone 16, rule from docs/findings.md §10.

The rule was applied by hand once, to five ladder rungs, and those decisions are recorded. The
strongest test available is that the code reaches the same decisions from the same numbers —
if it did not, the automation would be wrong, not the history. The numbers below are §10's table
converted to counts: 230 answerable gold items, band sizes as recorded.

§10 ranked on `binary:logistic`, the reported default at the time; the gate ranks on `rank:map`,
the default since. The two recorded decisions tested here come out the same either way.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src import evaluate
from src.promote import HOLD, PROMOTE, REGRESS, Scorecard, decide, gold_scorecard_part

ANSWERABLE = 230


def card(oof_binary, rank_top1, band_n, band_precision, rank_brier, oof_rank=0.9900):
    return Scorecard(
        oof_top1={"binary": oof_binary, "rank": oof_rank},
        gold_misses=round(ANSWERABLE * (1 - rank_top1)),
        gold_answerable=ANSWERABLE,
        band_wrong=round(band_n * (1 - band_precision)),
        band_n=band_n,
        gold_brier=rank_brier,
    )


# §10's table, corrected gold set. OOF for v5 is the unrounded regression the text quotes
# (0.106pp), since the table's 4-decimal rounding would overstate it.
V1 = card(0.9913, 0.9913, 168, 0.9762, 0.0221)
V4 = card(0.9908, 0.9870, 173, 0.9827, 0.0083)
V5 = card(0.9913 - 0.00106, 0.9913, 183, 0.9781, 0.0080)


def test_counts_match_the_recorded_table():
    assert (V1.gold_misses, V4.gold_misses, V5.gold_misses) == (2, 3, 2)
    assert (V1.band_wrong, V4.band_wrong, V5.band_wrong) == (4, 3, 4)


def test_v5_is_ineligible_by_its_oof_regression():
    """Missed the 0.1pp gate by 0.006pp. The gate was not moved then and is not moved here."""
    verdict = decide(V5, V1)
    assert verdict.outcome == REGRESS
    assert "ineligible" in verdict.reasons[0]


def test_v4_beats_v1_on_brier_after_two_ties():
    verdict = decide(V4, V1)
    assert verdict.outcome == PROMOTE
    assert "within ±2" in verdict.reasons[-3]  # gold top-1: 3 vs 2 misses
    assert "within ±2" in verdict.reasons[-2]  # band: 3 vs 4 wrong rows
    assert "lower" in verdict.reasons[-1]


def test_an_identical_model_is_held_not_promoted():
    """Retraining the champion's own code on the champion's own data should never move the alias."""
    verdict = decide(V4, V4)
    assert verdict.outcome == HOLD


def test_either_objective_can_make_a_challenger_ineligible():
    worse_rank = replace(V4, oof_top1={"binary": 0.9908, "rank": 0.9900 - 0.002})
    assert decide(worse_rank, V4).outcome == REGRESS


def test_a_drop_of_exactly_the_tolerance_is_eligible():
    at_limit = replace(V4, oof_top1={"binary": 0.9908 - 0.001, "rank": 0.9900})
    assert decide(at_limit, V4).outcome != REGRESS


@pytest.mark.parametrize(("delta", "expected"), [(3, REGRESS), (2, HOLD), (-2, HOLD), (-3, PROMOTE)])
def test_gold_top1_band(delta, expected):
    # Brier held equal so a tie on top-1 falls through to HOLD, not to a Brier win.
    challenger = replace(V4, gold_misses=V4.gold_misses + delta)
    assert decide(challenger, V4).outcome == expected


@pytest.mark.parametrize(("delta", "expected"), [(3, HOLD), (-3, PROMOTE), (2, HOLD)])
def test_band_rows_hold_rather_than_regress(delta, expected):
    """A worse band is not better, but it is not a regression the pipeline should go red for."""
    challenger = replace(V4, band_wrong=V4.band_wrong + delta)
    assert decide(challenger, V4).outcome == expected


def test_gold_scorecard_counts_items_and_band_rows():
    features = pd.DataFrame({
        "wikidata_qid":     ["Q1", "Q1", "Q2", "Q2", "Q3", "Q3"],
        "inat_taxon_id":    ["1", "2", "3", "4", "5", "6"],
        "label":            [1, 0, 0, 1, 0, 0],   # Q3 has no answer: not answerable
        "binary_raw_score": [0.99, 0.10, 0.97, 0.20, 0.96, 0.50],
        "rank_raw_score":   [2.0, 1.0, 3.0, 1.0, 1.0, 0.0],  # Q2 is a miss
        "rank_calibrated_prob": [0.9, 0.1, 0.8, 0.2, 0.3, 0.1],
    })
    part = gold_scorecard_part(features, brier=0.05)
    assert part == {
        "gold_misses": 1,
        "gold_answerable": 2,
        "band_wrong": 2,  # Q2's 0.97 and Q3's 0.96 are in [0.95, 1.01) and wrong
        "band_n": 3,
        "gold_brier": 0.05,
    }


def test_score_gold_with_model_uses_the_model_it_is_given(monkeypatch):
    """The gate scores a challenger that is not the champion; resolution must not be consulted."""
    from src import tracking

    monkeypatch.setattr(tracking, "resolve_model", lambda o: pytest.fail("resolve_model was called"))

    class Fixed:
        def score(self, features):
            return np.full(len(features), 0.7), np.full(len(features), 0.6)

    raw, calibrated = evaluate.score_gold_with_model(pd.DataFrame({"x": [1, 2]}), "binary", Fixed())
    assert raw.tolist() == [0.7, 0.7]
    assert calibrated.tolist() == [0.6, 0.6]
