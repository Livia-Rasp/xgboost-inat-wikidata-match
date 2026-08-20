"""Positive/negative label construction from Wikidata P3151, and the family grouping key used
for GroupKFold splits. See docs/inat-wikidata-match-spec.md §3 and §7 milestone 4.
"""

from __future__ import annotations

import random

import pandas as pd

# WD rank_qid -> comparable rank name. Derived empirically rather than hardcoded from memory:
# cross-tabulated every WD item's rank_qid against its *true* iNat taxon's known rank (joining
# through P3151 for the ~51k resolvable items) and took the modal match. Every entry here was
# independently >=92% pure against >=1 real sample, and every entry that overlaps with
# wikidata-inat-checker's own WD_RANK_LABELS (lib/utils.js) matches it exactly.
WD_RANK_TO_NAME = {
    "Q36732": "kingdom",
    "Q38348": "phylum",
    "Q2361851": "phylum",
    "Q334460": "phylum",
    "Q1153785": "subphylum",
    "Q37517": "class",
    "Q5867051": "subclass",
    "Q5868144": "superorder",
    "Q36602": "order",
    "Q5867959": "suborder",
    "Q21061732": "infraorder",
    "Q2889003": "infraorder",
    "Q2136103": "superfamily",
    "Q35409": "family",
    "Q164280": "subfamily",
    "Q227936": "tribe",
    "Q3965313": "subtribe",
    "Q34740": "genus",
    "Q3238261": "subgenus",
    "Q3181348": "section",
    "Q10861426": "zoosection",
    "Q6311258": "zoosubsection",
    "Q7432": "species",
    "Q68947": "subspecies",
    "Q767728": "variety",
    "Q4150646": "variety",
    "Q279749": "form",
}

# Ordinal rank scale for rank_level_diff. Reused from wikidata-inat-checker's own RANK_ORDER
# (lib/getInatTaxaDb.js) rather than invented fresh — already verified in production there.
# Higher number = finer/more specific rank. Only relative distance matters for a *_diff feature.
RANK_LEVEL = {
    "stateofmatter": 0, "kingdom": 10, "subkingdom": 11, "phylum": 20, "subphylum": 21,
    "superclass": 25, "class": 30, "subclass": 31, "infraclass": 32, "superorder": 35,
    "order": 40, "suborder": 41, "infraorder": 42, "parvorder": 43, "zoosection": 44,
    "zoosubsection": 45, "superfamily": 46, "epifamily": 47, "family": 50, "subfamily": 51,
    "supertribe": 52, "tribe": 53, "subtribe": 54, "genus": 60, "genushybrid": 61,
    "subgenus": 62, "section": 63, "subsection": 64, "complex": 65, "species": 70,
    "hybrid": 71, "subspecies": 80, "variety": 81, "form": 82,
}

# Preference order when an item isn't family-rank (or finer) itself: nearest coarser standard
# rank among its ancestors, family first.
FAMILY_FALLBACK_RANKS = ("family", "order", "class", "kingdom")
_FAMILY_LEVEL = RANK_LEVEL["family"]

SYNTHETIC_DROPOUT_FRACTION = 0.15
RANDOM_SEED = 42


def build_family_keys(wikidata_taxa: pd.DataFrame, ancestors: pd.DataFrame) -> pd.Series:
    """Family (or nearest coarser available rank) for every Wikidata item, from its own P171
    ancestor chain — independent of whether its P3151 link resolves, so every item gets a
    grouping key uniformly (this is why the ancestor-chain pull was worth the extra ~15-30 min:
    it replaces a heuristic fallback for the 12.85% stale-P3151 items with a real one).
    Returns a Series indexed by qid."""
    own_rank_name = wikidata_taxa.set_index("qid")["rank_qid"].map(WD_RANK_TO_NAME)

    keys: dict[str, str] = {}
    for qid, rank_name in own_rank_name.items():
        if pd.notna(rank_name) and RANK_LEVEL.get(rank_name, 999) <= _FAMILY_LEVEL:
            keys[qid] = qid

    anc = ancestors.copy()
    anc["rank_name"] = anc["ancestor_rank_qid"].map(WD_RANK_TO_NAME)
    anc = anc[anc["rank_name"].isin(FAMILY_FALLBACK_RANKS) & ~anc["qid"].isin(keys.keys())]
    if not anc.empty:
        anc["rank_level"] = anc["rank_name"].map(RANK_LEVEL)
        best = anc.loc[anc.groupby("qid")["rank_level"].idxmax()]
        for qid, ancestor_qid in zip(best["qid"], best["ancestor_qid"]):
            keys[qid] = ancestor_qid

    # Ultimate fallback (no family-or-coarser rank anywhere in reach): own qid, an ungrouped
    # singleton. Rare — 349/51,310 resolvable items (0.68%) needed it in the milestone 4 plan's
    # exploration, all above-family taxa or genuine taxonomic gaps.
    return wikidata_taxa["qid"].apply(lambda q: keys.get(q, q)).set_axis(wikidata_taxa["qid"])


def build_labels(candidates: pd.DataFrame, wikidata_taxa: pd.DataFrame) -> pd.DataFrame:
    """Positive: the candidate whose taxon_id equals the item's true inat_id (from P3151).
    Negative: every other candidate in the group. No synthetic dropout applied here — see
    apply_synthetic_dropout()."""
    df = candidates.copy()
    true_by_qid = wikidata_taxa.set_index("qid")["inat_id"]
    df["true_inat_id"] = df["wikidata_qid"].map(true_by_qid)
    df["label"] = (df["inat_taxon_id"] == df["true_inat_id"]).astype(int)
    return df


def apply_synthetic_dropout(
    labeled: pd.DataFrame,
    wikidata_taxa: pd.DataFrame,
    fraction: float = SYNTHETIC_DROPOUT_FRACTION,
    seed: int = RANDOM_SEED,
) -> tuple[pd.DataFrame, pd.Series]:
    """Spec §3's abstention-training mechanic: randomly drop the true candidate from a fraction
    of the groups that have one, leaving an all-negative group. Distinguished from the
    12.85%-of-items whose group is *naturally* all-negative because their P3151 link is stale
    (milestone 3's finding) — both end up all-negative, but for different reasons, so
    no_answer_reason keeps them separable for spec §6's per-reason abstention evaluation.
    Returns (labeled_with_dropout_applied, no_answer_reason indexed by qid)."""
    has_positive = labeled.groupby("wikidata_qid")["label"].max()
    resolvable_qids = has_positive[has_positive == 1].index.tolist()

    rng = random.Random(seed)
    n_drop = int(len(resolvable_qids) * fraction)
    dropout_qids = set(rng.sample(resolvable_qids, n_drop))

    df = labeled.copy()
    drop_mask = df["wikidata_qid"].isin(dropout_qids) & (df["label"] == 1)
    df.loc[drop_mask, "label"] = 0

    reason = pd.Series("stale_p3151", index=wikidata_taxa["qid"].values)
    reason.loc[reason.index.isin(resolvable_qids)] = "has_positive"
    reason.loc[reason.index.isin(dropout_qids)] = "synthetic_dropout"
    return df, reason
