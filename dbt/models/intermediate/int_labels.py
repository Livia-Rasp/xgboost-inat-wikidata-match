"""Labels, plus spec §3's synthetic abstention dropout.

A Python model rather than SQL, deliberately. Two of the three steps are pure joins and would be
one-liners in SQL, but the third — dropping the true candidate from a seeded 15% of resolvable
groups — is `random.Random(42).sample()` over a sorted list, and Python's Mersenne Twister has no
SQL equivalent. platform-design §4.3.2 originally accepted a hash-modulo selection as documented
drift; keeping the real function instead means `label` and `no_answer_reason` are *identical*
between the two implementations, so the parity report measures string similarity and tie-break
ordering rather than 15% of the groups being relabelled for an unrelated reason.

src/labels.py stays the single source of truth. Nothing here reimplements it.
"""

from src.labels import apply_synthetic_dropout, build_labels


def model(dbt, session):
    dbt.config(materialized="table")

    candidates = dbt.ref("stg_candidates").df()
    # build_labels/apply_synthetic_dropout are written against the raw cache column names.
    wikidata_taxa = (
        dbt.ref("stg_wd_taxa")
        .df()
        .rename(columns={"wikidata_qid": "qid", "true_inat_id": "inat_id"})
    )

    labeled = build_labels(candidates, wikidata_taxa)
    labeled, no_answer_reason = apply_synthetic_dropout(labeled, wikidata_taxa)

    labeled["no_answer_reason"] = labeled["wikidata_qid"].map(no_answer_reason)
    return labeled[
        ["wikidata_qid", "inat_taxon_id", "true_inat_id", "label", "no_answer_reason"]
    ]
