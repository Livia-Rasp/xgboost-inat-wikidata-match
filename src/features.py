"""Pairwise feature construction, plus GroupKFold splits by family. See
docs/inat-wikidata-match-spec.md §4 and §7 milestone 4.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
from rapidfuzz.distance import JaroWinkler, Levenshtein
from sklearn.model_selection import GroupKFold

from .candidates import DEFAULT_CACHE_PATH as LOOKUP_SQLITE_PATH
from .candidates import STRATEGY_TAGS
from .labels import RANK_LEVEL, WD_RANK_TO_NAME, apply_synthetic_dropout, build_family_keys, build_labels
from .normalize import normalize_name

N_SPLITS = 5
RANDOM_STATE = 42

# Standard ranks compared between the WD ancestor chain and the iNat ancestry chain.
COMPARISON_RANKS = ("kingdom", "family", "order")

DEFAULT_FEATURES_PATH = Path(__file__).resolve().parent.parent / "data" / "features.parquet"
DEFAULT_FEATURES_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "features.manifest.json"
)


def _load_inat_index(lookup_sqlite_path: Path = LOOKUP_SQLITE_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{lookup_sqlite_path}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(
            "SELECT taxon_id, name, rank, ancestry, genus, specific_epithet FROM taxa_normalized",
            conn,
        )
    finally:
        conn.close()


def _wd_ancestor_names_by_rank(
    wikidata_taxa: pd.DataFrame, ancestors: pd.DataFrame, target_ranks=COMPARISON_RANKS
) -> dict[str, dict[str, str]]:
    """{qid: {rank_name: ancestor_or_self_name}} — an item at (or above) a target rank counts as
    its own ancestor at that rank, same convention as build_family_keys."""
    own_rank_name = wikidata_taxa.set_index("qid")["rank_qid"].map(WD_RANK_TO_NAME)
    own_name = wikidata_taxa.set_index("qid")["name"]

    result: dict[str, dict[str, str]] = {}
    for qid, rank_name in own_rank_name.items():
        if rank_name in target_ranks:
            result.setdefault(qid, {})[rank_name] = own_name.get(qid)

    anc = ancestors.copy()
    anc["rank_name"] = anc["ancestor_rank_qid"].map(WD_RANK_TO_NAME)
    anc = anc[anc["rank_name"].isin(target_ranks)]
    for qid, rank_name, name in zip(anc["qid"], anc["rank_name"], anc["ancestor_name"]):
        result.setdefault(qid, {}).setdefault(rank_name, name)
    return result


def _inat_ancestor_names_by_rank(
    inat_index: pd.DataFrame, taxon_ids: set[str] | None = None, target_ranks=COMPARISON_RANKS
) -> dict[str, dict[str, str]]:
    """{taxon_id: {rank_name: ancestor_or_self_name}}, walking each row's ancestry string.

    `taxon_ids` restricts which rows get walked — pass the distinct candidate taxon_ids, not the
    full 1.4M-row index: only a few tens of thousands of those ever appear as candidates, so
    walking every row (most of which are never looked up) would be pure waste at full scale.
    `id_to_name_rank` still needs the full index, since a walk can reach any node as an ancestor.
    """
    id_to_name_rank = dict(zip(inat_index["taxon_id"], zip(inat_index["name"], inat_index["rank"])))
    rows = inat_index if taxon_ids is None else inat_index[inat_index["taxon_id"].isin(taxon_ids)]

    result: dict[str, dict[str, str]] = {}
    for taxon_id, name, rank, ancestry in rows[["taxon_id", "name", "rank", "ancestry"]].itertuples(
        index=False
    ):
        found: dict[str, str] = {}
        if rank in target_ranks:
            found[rank] = name
        if isinstance(ancestry, str) and len(found) < len(target_ranks):
            for aid in ancestry.split("/"):
                anr = id_to_name_rank.get(aid)
                if anr and anr[1] in target_ranks and anr[1] not in found:
                    found[anr[1]] = anr[0]
                    if len(found) == len(target_ranks):
                        break
        result[taxon_id] = found
    return result


def _jw(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return JaroWinkler.normalized_similarity(a, b)


def build_features(
    candidates: pd.DataFrame,
    wikidata_taxa: pd.DataFrame,
    ancestors: pd.DataFrame,
    inat_index: pd.DataFrame,
) -> pd.DataFrame:
    """One row per candidate pair. See spec §4 for the feature groups."""
    df = candidates.merge(
        inat_index[["taxon_id", "ancestry", "genus", "specific_epithet"]].rename(
            columns={"genus": "inat_genus", "specific_epithet": "inat_epithet"}
        ),
        left_on="inat_taxon_id",
        right_on="taxon_id",
        how="left",
    ).drop(columns=["taxon_id"])
    df = df.merge(
        wikidata_taxa[["qid", "name", "rank_qid", "parent_name", "sitelinks", "statements", "iucn_qid", "has_commons_cat"]],
        left_on="wikidata_qid",
        right_on="qid",
        how="left",
        suffixes=("", "_wd"),
    ).drop(columns=["qid"])

    wd_norm = df["name"].apply(normalize_name)
    inat_norm = df["inat_name"].apply(normalize_name)

    # ---- String similarity (spec §4) ----
    df["name_exact_raw"] = df["name"] == df["inat_name"]
    df["name_exact_norm"] = [a.normalized == b.normalized for a, b in zip(wd_norm, inat_norm)]
    df["jaro_winkler_full"] = [_jw(a.normalized, b.normalized) for a, b in zip(wd_norm, inat_norm)]
    df["levenshtein_ratio_full"] = [
        Levenshtein.normalized_similarity(a.normalized, b.normalized) if a.normalized and b.normalized else 0.0
        for a, b in zip(wd_norm, inat_norm)
    ]
    df["genus_exact"] = [a.genus == b.genus and a.genus is not None for a, b in zip(wd_norm, inat_norm)]
    df["genus_jw"] = [_jw(a.genus, b.genus) for a, b in zip(wd_norm, inat_norm)]
    df["epithet_exact"] = [
        a.specific_epithet == b.specific_epithet and a.specific_epithet is not None
        for a, b in zip(wd_norm, inat_norm)
    ]
    df["epithet_jw"] = [_jw(a.specific_epithet, b.specific_epithet) for a, b in zip(wd_norm, inat_norm)]
    df["epithet_stem_match"] = [
        a.epithet_stem == b.epithet_stem and a.epithet_stem is not None for a, b in zip(wd_norm, inat_norm)
    ]
    df["token_count_diff"] = [
        abs(len(a.normalized.split()) - len(b.normalized.split())) for a, b in zip(wd_norm, inat_norm)
    ]
    df["length_diff"] = [abs(len(a.normalized) - len(b.normalized)) for a, b in zip(wd_norm, inat_norm)]
    df["infra_rank_match"] = [a.infraspecific_rank == b.infraspecific_rank for a, b in zip(wd_norm, inat_norm)]
    df["infra_epithet_match"] = [
        a.infraspecific_epithet == b.infraspecific_epithet and a.infraspecific_epithet is not None
        for a, b in zip(wd_norm, inat_norm)
    ]
    df["hybrid_flag_match"] = [a.hybrid == b.hybrid for a, b in zip(wd_norm, inat_norm)]

    # ---- Taxonomic agreement ----
    wd_rank_name = df["rank_qid"].map(WD_RANK_TO_NAME)
    df["rank_equal"] = wd_rank_name == df["inat_rank"]
    df["rank_level_diff"] = [
        abs(RANK_LEVEL[w] - RANK_LEVEL[i]) if w in RANK_LEVEL and i in RANK_LEVEL else None
        for w, i in zip(wd_rank_name, df["inat_rank"])
    ]

    wd_ranks = _wd_ancestor_names_by_rank(wikidata_taxa, ancestors)
    inat_ranks = _inat_ancestor_names_by_rank(inat_index, taxon_ids=set(df["inat_taxon_id"]))
    for rank in COMPARISON_RANKS:
        wd_names = df["wikidata_qid"].map(lambda q: wd_ranks.get(q, {}).get(rank))
        inat_names = df["inat_taxon_id"].map(lambda t: inat_ranks.get(t, {}).get(rank))
        df[f"{rank}_match"] = [
            (w is not None and w == i) for w, i in zip(wd_names, inat_names)
        ]
    df["shared_ancestor_depth"] = sum(df[f"{r}_match"].astype(int) for r in COMPARISON_RANKS)
    df["parent_name_jw"] = [_jw(p, n) for p, n in zip(df["parent_name"], df["inat_name"])]
    df["inat_active"] = True  # constant by construction (only active taxa are indexed) — no signal here

    # ---- Group context ----
    group_sizes = df.groupby("wikidata_qid")["inat_taxon_id"].transform("size")
    df["n_candidates"] = group_sizes
    inat_name_counts = inat_index["name"].value_counts()
    df["n_inat_taxa_same_name"] = df["inat_name"].map(inat_name_counts).fillna(0).astype(int)
    wd_name_counts = wikidata_taxa["name"].value_counts()
    df["n_wikidata_items_same_name"] = df["name"].map(wd_name_counts).fillna(0).astype(int)
    df["sim_rank_in_group"] = df.groupby("wikidata_qid")["similarity"].rank(ascending=False, method="first")
    top2 = df.groupby("wikidata_qid")["similarity"].transform(lambda s: s.nlargest(2).min() if len(s) > 1 else s.iloc[0])
    df["sim_margin_to_runner_up"] = df["similarity"] - top2
    for tag in STRATEGY_TAGS:
        df[f"strategy_{tag}"] = df["strategies"].str.contains(tag, regex=False)

    # ---- Popularity / quality ----
    df["wikidata_sitelink_count"] = df["sitelinks"]
    df["wikidata_statement_count"] = df["statements"]
    df["wikidata_has_iucn"] = df["iucn_qid"].notna()
    df["wikidata_has_commons_cat"] = df["has_commons_cat"]
    # log1p(inat_observation_count) skipped — spec marks it optional/no-API-calls, and it needs a
    # separate iNat open-data download (observations.csv) this project hasn't pulled.

    return df.drop(
        columns=["name", "rank_qid", "parent_name", "sitelinks", "statements", "iucn_qid", "has_commons_cat", "ancestry", "inat_genus", "inat_epithet"]
    )


def _features_manifest_matches(manifest_path: Path, shape_key: dict) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return manifest == shape_key


def build_features_and_splits(
    candidates: pd.DataFrame,
    wikidata_taxa: pd.DataFrame,
    ancestors: pd.DataFrame,
    inat_index: pd.DataFrame,
    features_path: Path = DEFAULT_FEATURES_PATH,
    manifest_path: Path = DEFAULT_FEATURES_MANIFEST_PATH,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Full pipeline: labels (+ synthetic dropout) + features + family_key + GroupKFold fold
    assignment, cached to parquet."""
    shape_key = {
        "n_candidates": len(candidates),
        "n_wikidata": len(wikidata_taxa),
        "n_ancestors": len(ancestors),
        "n_inat_index": len(inat_index),
        "n_splits": N_SPLITS,
    }
    if not force_refresh and features_path.exists() and _features_manifest_matches(manifest_path, shape_key):
        return pd.read_parquet(features_path)

    labeled = build_labels(candidates, wikidata_taxa)
    labeled, no_answer_reason = apply_synthetic_dropout(labeled, wikidata_taxa)

    df = build_features(labeled, wikidata_taxa, ancestors, inat_index)
    df["label"] = labeled["label"].values
    df["no_answer_reason"] = df["wikidata_qid"].map(no_answer_reason)

    family_keys = build_family_keys(wikidata_taxa, ancestors)
    df["family_key"] = df["wikidata_qid"].map(family_keys)

    gkf = GroupKFold(n_splits=N_SPLITS, shuffle=True, random_state=RANDOM_STATE)
    fold_of_qid = {}
    qid_family = wikidata_taxa[["qid"]].copy()
    qid_family["family_key"] = qid_family["qid"].map(family_keys)
    for fold, (_, test_idx) in enumerate(
        gkf.split(qid_family, groups=qid_family["family_key"])
    ):
        for qid in qid_family.iloc[test_idx]["qid"]:
            fold_of_qid[qid] = fold
    df["fold"] = df["wikidata_qid"].map(fold_of_qid)

    features_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(features_path, index=False)
    manifest_path.write_text(json.dumps(shape_key, indent=2))
    return df


def verify_no_qid_split_across_folds(df: pd.DataFrame) -> bool:
    """Spec §7 milestone 4's literal check."""
    return (df.groupby("wikidata_qid")["fold"].nunique() == 1).all()


if __name__ == "__main__":
    from .candidates import DEFAULT_CANDIDATES_PATH, build_lookup_cache
    from .wikidata import DEFAULT_ANCESTORS_CACHE_PATH, DEFAULT_CACHE_PATH as WIKIDATA_TAXA_PATH

    wikidata_taxa = pd.read_parquet(WIKIDATA_TAXA_PATH)
    candidates = pd.read_parquet(DEFAULT_CANDIDATES_PATH)
    ancestors = pd.read_parquet(DEFAULT_ANCESTORS_CACHE_PATH)
    build_lookup_cache()  # ensure it exists; we read it directly below
    inat_index = _load_inat_index()

    df = build_features_and_splits(candidates, wikidata_taxa, ancestors, inat_index)
    print(f"{len(df):,} feature rows, {df.shape[1]} columns")

    ok = verify_no_qid_split_across_folds(df)
    print(f"no QID split across folds: {ok}")

    print("\nfold sizes:")
    print(df.groupby("fold")["wikidata_qid"].nunique())

    print("\nno_answer_reason breakdown (per item):")
    print(df.drop_duplicates("wikidata_qid")["no_answer_reason"].value_counts())

    print(f"\ndistinct family_key groups: {df['family_key'].nunique():,}")
