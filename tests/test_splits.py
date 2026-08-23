"""The leakage regression (spec §7 milestone 4).

Every candidate row for one Wikidata item must land in one fold. If an item's rows were split
across folds, the model would be validated on rows whose siblings it trained on — the group
context features (`sim_rank_in_group`, `sim_margin_to_runner_up`) are computed *within* an item,
so leakage there is not subtle, it is direct.

`features.verify_no_qid_split_across_folds()` has always existed, but only as a `print` in a
`__main__` block that nobody runs in CI. This is that check as something that can fail.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.features import DEFAULT_FEATURES_PATH, N_SPLITS, verify_no_qid_split_across_folds


def _frame(fold_of_qid: dict[str, int]) -> pd.DataFrame:
    """Several candidate rows per item, as the real feature frame has."""
    return pd.DataFrame([
        {"wikidata_qid": qid, "inat_taxon_id": str(i), "fold": fold}
        for qid, fold in fold_of_qid.items()
        for i in range(3)
    ])


def test_clean_fold_assignment_passes():
    assert verify_no_qid_split_across_folds(_frame({"Q1": 0, "Q2": 1, "Q3": 2}))


def test_a_qid_spanning_two_folds_fails():
    """The check has to be able to fail, or it is decoration."""
    df = _frame({"Q1": 0, "Q2": 1})
    df.loc[0, "fold"] = 4  # one of Q1's three rows moved to another fold
    assert not verify_no_qid_split_across_folds(df)


def test_grouping_keeps_a_family_together():
    """GroupKFold on family_key is what produces the property above: items are assigned a fold
    through their family, so two items in one family cannot be split, let alone one item."""
    from sklearn.model_selection import GroupKFold

    qids = pd.DataFrame({
        "qid": [f"Q{i}" for i in range(20)],
        "family_key": [f"fam{i % 4}" for i in range(20)],
    })
    gkf = GroupKFold(n_splits=4, shuffle=True, random_state=42)

    fold_of_qid = {}
    for fold, (_, test_idx) in enumerate(gkf.split(qids, groups=qids["family_key"])):
        for qid in qids.iloc[test_idx]["qid"]:
            fold_of_qid[qid] = fold

    assert verify_no_qid_split_across_folds(_frame(fold_of_qid))
    families_per_fold = (
        qids.assign(fold=qids["qid"].map(fold_of_qid)).groupby("family_key")["fold"].nunique()
    )
    assert (families_per_fold == 1).all()


@pytest.mark.skipif(
    not DEFAULT_FEATURES_PATH.exists(),
    reason="needs the full data/features.parquet — built by `python -m src.features`",
)
def test_the_real_feature_frame_has_no_leakage():
    """Runs locally where the full pipeline has been built, skipped in CI where it has not.
    The synthetic tests above prove the check works; this one proves the actual data passes it."""
    features = pd.read_parquet(DEFAULT_FEATURES_PATH, columns=["wikidata_qid", "fold"])
    assert verify_no_qid_split_across_folds(features)
    assert features["fold"].nunique() == N_SPLITS
