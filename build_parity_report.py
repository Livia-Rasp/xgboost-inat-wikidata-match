"""Diff the dbt-built feature table against the pandas one, column by column.

Spec §7 milestone 14's acceptance check: "a parity report compares the SQL-built feature table
against the pandas one column by column, with a written explanation for every column that
differs." This produces the measurement; the explanations live in docs/findings.md §9.

Needs both frames on disk — `make features` and `make features-sql`:

    .venv/bin/python build_parity_report.py

Prints a markdown table (columns that differ, then a one-line all-clear for the rest) plus the
supporting counts each written cause rests on, so the numbers quoted in findings.md can be
re-derived rather than taken on trust.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from src.features import DEFAULT_FEATURES_PATH
from src.labels import WD_RANK_TO_NAME
from src.paths import DATA_DIR
from src.train import FEATURE_COLUMNS

DBT_FEATURES_PATH = DATA_DIR / "features_dbt.parquet"
KEY = ["wikidata_qid", "inat_taxon_id"]

# One ULP of a 64-bit float is ~2.2e-16 near 1.0. Anything under this is the two libraries
# rounding the same arithmetic differently, not a different answer — see docs/findings.md §9.
FLOAT_NOISE = 1e-9

COMPARISON_RANKS = ("kingdom", "family", "order")


def load_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    for path, target in ((DEFAULT_FEATURES_PATH, "make features"), (DBT_FEATURES_PATH, "make features-sql")):
        if not path.exists():
            sys.exit(f"{path} is missing — run `{target}` first.")

    pandas_features = pd.read_parquet(DEFAULT_FEATURES_PATH).sort_values(KEY).reset_index(drop=True)
    dbt_features = pd.read_parquet(DBT_FEATURES_PATH).sort_values(KEY).reset_index(drop=True)

    if not pandas_features[KEY].equals(dbt_features[KEY]):
        sys.exit("the two frames do not cover the same candidate pairs — that is a bug, not drift.")
    return pandas_features, dbt_features


def compare_column(left: pd.Series, right: pd.Series) -> tuple[int, float]:
    """(rows that differ, max absolute delta). Floats are compared with a tolerance, since the
    interesting question for them is whether a *different* number was computed, not whether the
    last bit of the mantissa agrees."""
    both_null = left.isna() & right.isna()
    if left.dtype.kind == "f" or right.dtype.kind == "f":
        delta = (left.astype("float64") - right.astype("float64")).abs()
        differs = (delta > FLOAT_NOISE) & ~both_null
        return int(differs.sum()), float(np.nanmax(delta.values)) if len(delta) else 0.0
    if left.dtype.kind in "iu":
        delta = (left.astype("int64") - right.astype("int64")).abs()
        return int((delta != 0).sum()), float(delta.max())
    differs = (left.astype(object) != right.astype(object)) & ~both_null
    return int(differs.sum()), float("nan")


def parity_table(pandas_features: pd.DataFrame, dbt_features: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in pandas_features.columns:
        n_differ, max_delta = compare_column(pandas_features[column], dbt_features[column])
        rows.append(
            {
                "column": column,
                "in_model": column in FEATURE_COLUMNS,
                "rows_differing": n_differ,
                "share": n_differ / len(pandas_features),
                "max_abs_delta": max_delta,
            }
        )
    return pd.DataFrame(rows)


def ancestor_ambiguity_qids(ancestors: pd.DataFrame) -> set[str]:
    """Items whose transitive P171 chain holds more than one distinct ancestor at a comparison
    rank — the population where §4.3.1's tie-break rule can possibly change an answer."""
    anc = ancestors.copy()
    anc["rank_name"] = anc["ancestor_rank_qid"].map(WD_RANK_TO_NAME)
    anc = anc[anc["rank_name"].isin(COMPARISON_RANKS)]
    counts = anc.groupby(["qid", "rank_name"])["ancestor_qid"].nunique()
    return set(counts[counts > 1].index.get_level_values(0))


def explain(pandas_features: pd.DataFrame, dbt_features: pd.DataFrame) -> None:
    """The supporting counts behind findings.md §9's written causes."""
    from src.wikidata import DEFAULT_ANCESTORS_CACHE_PATH

    print("\n### Supporting counts\n")

    ambiguous = ancestor_ambiguity_qids(pd.read_parquet(DEFAULT_ANCESTORS_CACHE_PATH))
    print(f"Items with >1 distinct ancestor at a comparison rank: {len(ambiguous):,}")
    for column in ("kingdom_match", "family_match", "order_match"):
        differing = pandas_features[pandas_features[column] != dbt_features[column]]
        inside = differing["wikidata_qid"].isin(ambiguous).mean() if len(differing) else 1.0
        print(
            f"  {column:<14} {len(differing):>7,} rows / {differing['wikidata_qid'].nunique():>6,} items"
            f"  — inside an ambiguous-chain item: {inside * 100:.1f}%"
        )

    # sim_rank_in_group: the rule only reorders candidates that scored identically, so every
    # differing row must sit inside a tie. If one ever does not, the rule changed the ranking
    # rather than the tie-break, which would be a bug.
    tie_size = pandas_features.groupby("wikidata_qid")["similarity"].transform(
        lambda s: s.map(s.value_counts())
    )
    rank_differs = pandas_features["sim_rank_in_group"] != dbt_features["sim_rank_in_group"]
    print(
        f"\nsim_rank_in_group: {int(rank_differs.sum()):,} rows differ, "
        f"all inside a similarity tie: {bool((tie_size[rank_differs] > 1).all())}"
    )
    margin_unchanged = np.allclose(
        pandas_features["sim_margin_to_runner_up"], dbt_features["sim_margin_to_runner_up"]
    )
    print(f"  sim_margin_to_runner_up unchanged: {bool(margin_unchanged)}")

    # The float columns: how much of the "difference" is below the noise floor.
    print("\nString-similarity columns, before the tolerance is applied:")
    for column in ("jaro_winkler_full", "levenshtein_ratio_full", "genus_jw", "epithet_jw", "parent_name_jw"):
        delta = (pandas_features[column] - dbt_features[column]).abs()
        print(
            f"  {column:<22} any difference: {int((delta > 0).sum()):>7,}"
            f"   above {FLOAT_NOISE:g}: {int((delta > FLOAT_NOISE).sum()):>6,}"
            f"   max |Δ|: {delta.max():.3e}"
        )

    # parent_name_jw is the only feature computed on raw rather than normalised names, and
    # normalize.py's genus/epithet patterns are ASCII-only — so it is the only place a non-ASCII
    # string reaches a similarity function, and the only place DuckDB's byte-oriented
    # implementation can disagree with rapidfuzz's code-point-oriented one.
    from src.wikidata import DEFAULT_CACHE_PATH as WIKIDATA_TAXA_PATH

    parent_names = (
        pd.read_parquet(WIKIDATA_TAXA_PATH, columns=["qid", "parent_name"])
        .set_index("qid")["parent_name"]
    )
    non_ascii = ~pandas_features["inat_name"].map(str.isascii) | ~pandas_features["wikidata_qid"].map(
        parent_names
    ).fillna("").map(str.isascii)
    disagree = (pandas_features["parent_name_jw"] - dbt_features["parent_name_jw"]).abs() > FLOAT_NOISE
    print(
        f"\nparent_name_jw disagreements where either raw string is non-ASCII: "
        f"{int((disagree & non_ascii).sum()):,} of {int(disagree.sum()):,}"
    )


def main() -> None:
    pandas_features, dbt_features = load_frames()
    print(f"{len(pandas_features):,} rows x {pandas_features.shape[1]} columns, both frames.\n")

    table = parity_table(pandas_features, dbt_features)
    differing = table[table["rows_differing"] > 0].sort_values("rows_differing", ascending=False)
    identical = table[table["rows_differing"] == 0]

    print("### Columns that differ\n")
    if differing.empty:
        print("None.")
    else:
        print("| column | in model | rows differing | share | max abs delta |")
        print("|---|---|---:|---:|---:|")
        for row in differing.itertuples(index=False):
            delta = "—" if np.isnan(row.max_abs_delta) else f"{row.max_abs_delta:.3g}"
            print(
                f"| `{row.column}` | {'yes' if row.in_model else 'no'} | {row.rows_differing:,} "
                f"| {row.share * 100:.2f}% | {delta} |"
            )

    print(f"\n### Columns that agree exactly: {len(identical)} of {len(table)}\n")
    print(", ".join(f"`{c}`" for c in identical["column"]))

    explain(pandas_features, dbt_features)


if __name__ == "__main__":
    main()
