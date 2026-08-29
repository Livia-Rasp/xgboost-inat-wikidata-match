"""Family grouping key and GroupKFold fold assignment, one row per Wikidata item.

A Python model on purpose, and the one platform-design §5.2 names explicitly: the leakage
guarantee — no QID, and in fact no whole family, appearing in two folds — is the property that
must not move across this migration, and there is nothing to gain from reimplementing
GroupKFold's balancing in SQL to prove it can be done.

build_family_keys is a genuine transformation and could have been SQL (own rank if family-or-
coarser, else the finest of family/order/class/kingdom in the P171 chain, else the item itself).
It stays here because the fold split needs it in the same process anyway, and splitting it across
two languages would put the group key and the splitter out of step for no benefit.
"""

import pandas as pd
from sklearn.model_selection import GroupKFold

from src.features import N_SPLITS, RANDOM_STATE
from src.labels import build_family_keys


def model(dbt, session):
    dbt.config(materialized="table")

    wikidata_taxa = dbt.ref("stg_wd_taxa").df().rename(columns={"wikidata_qid": "qid"})
    ancestors = dbt.ref("stg_wd_ancestors").df().rename(columns={"wikidata_qid": "qid"})

    family_keys = build_family_keys(wikidata_taxa, ancestors)

    qid_family = wikidata_taxa[["qid"]].copy()
    qid_family["family_key"] = qid_family["qid"].map(family_keys)

    gkf = GroupKFold(n_splits=N_SPLITS, shuffle=True, random_state=RANDOM_STATE)
    fold_of_qid: dict[str, int] = {}
    for fold, (_, test_idx) in enumerate(gkf.split(qid_family, groups=qid_family["family_key"])):
        for qid in qid_family.iloc[test_idx]["qid"]:
            fold_of_qid[qid] = fold

    return pd.DataFrame(
        {
            "wikidata_qid": qid_family["qid"],
            "family_key": qid_family["family_key"],
            "fold": qid_family["qid"].map(fold_of_qid).astype("int64"),
        }
    )
