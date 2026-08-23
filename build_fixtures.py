"""Regenerate the committed fixtures in tests/fixtures/ from the full caches in data/.

Two audiences:

  * `src/evaluate.py --gold` on a clean clone — the "five-minute path" in the README. Needs the
    slice of the iNat index the gold candidates touch, the two small Wikidata pulls for those
    items, and a summary of the OOF predictions.
  * the test suite — a tiny hand-written taxa fixture, which is *not* generated here. That one
    lives at tests/fixtures/taxa_mini.csv and is edited by hand, because a test fixture derived
    from live data stops being a fixed point the moment the data moves.

Run after anything that changes the gold set:

    .venv/bin/python build_fixtures.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from src.candidates import DEFAULT_CACHE_PATH as LOOKUP_SQLITE_PATH
from src.evaluate import GOLD_ANCESTORS_PATH, GOLD_HARD_CASES_PATH, oof_reference
from src.features import INAT_INDEX_COLUMNS
from src.fixtures import (
    GOLD_ANCESTORS_FIXTURE,
    GOLD_ATTRIBUTES_FIXTURE,
    GOLD_INAT_INDEX_FIXTURE,
    OOF_SUMMARY_FIXTURE,
)
from src.train import DEFAULT_OOF_PATH
from src.wikidata import DEFAULT_GOLD_ATTRIBUTES_PATH

# build_features() computes n_wikidata_items_same_name from this frame's own name counts, so the
# columns kept here have to be exactly the ones it reads. synonym_names/basionym_names feed
# candidate generation only, and are list-valued, so they are dropped rather than mangled through
# CSV. See src/fixtures.py.
ATTRIBUTE_COLUMNS = [
    "qid", "name", "rank_qid", "parent_qid", "parent_name",
    "iucn_qid", "sitelinks", "statements", "has_commons_cat",
]


def _require(path: Path, how: str) -> None:
    if not path.exists():
        raise SystemExit(f"{path} not found — {how}")


def build_inat_index_fixture(hard_cases: pd.DataFrame) -> pd.DataFrame:
    """The iNat rows the gold path actually reads, and no more. Three groups, and each is needed
    for a specific reason:

      * every candidate taxon in gold/hard_cases.csv — the rows being scored;
      * every row *sharing a name* with one of those — otherwise n_inat_taxa_same_name, which
        counts name collisions across the whole index, would be silently wrong rather than
        merely absent;
      * every ancestor reachable from a candidate's ancestry string — the kingdom/family/order
        walk resolves ancestor ids against this same table.

    ~4k rows out of 1.4M, which is why this is committable at all."""
    _require(LOOKUP_SQLITE_PATH, "run `python -m src.candidates` to build it")
    conn = sqlite3.connect(f"file:{LOOKUP_SQLITE_PATH}?mode=ro", uri=True)
    try:
        cols = ", ".join(INAT_INDEX_COLUMNS)
        ids = sorted(set(hard_cases["inat_taxon_id"].astype(str)))
        placeholders = ",".join("?" * len(ids))
        candidates = pd.read_sql_query(
            f"SELECT {cols} FROM taxa_normalized WHERE taxon_id IN ({placeholders})", conn, params=ids
        )

        names = sorted(set(candidates["name"]))
        same_name = pd.read_sql_query(
            f"SELECT {cols} FROM taxa_normalized WHERE name IN ({','.join('?' * len(names))})",
            conn, params=names,
        )

        ancestor_ids = sorted({
            aid for ancestry in candidates["ancestry"].dropna() for aid in str(ancestry).split("/") if aid
        })
        ancestors = pd.read_sql_query(
            f"SELECT {cols} FROM taxa_normalized WHERE taxon_id IN ({','.join('?' * len(ancestor_ids))})",
            conn, params=ancestor_ids,
        )
    finally:
        conn.close()

    combined = pd.concat([candidates, same_name, ancestors], ignore_index=True)
    return combined.drop_duplicates("taxon_id").sort_values("taxon_id").reset_index(drop=True)


def main() -> None:
    _require(GOLD_HARD_CASES_PATH, "run `python build_gold_set.py` first")
    hard_cases = pd.read_csv(GOLD_HARD_CASES_PATH, dtype={"inat_taxon_id": str})
    GOLD_INAT_INDEX_FIXTURE.parent.mkdir(parents=True, exist_ok=True)

    inat_index = build_inat_index_fixture(hard_cases)
    inat_index.to_csv(GOLD_INAT_INDEX_FIXTURE, index=False, compression="gzip")

    _require(DEFAULT_GOLD_ATTRIBUTES_PATH, "run `python build_gold_set.py` first")
    attributes = pd.read_parquet(DEFAULT_GOLD_ATTRIBUTES_PATH)[ATTRIBUTE_COLUMNS]
    attributes.sort_values("qid").to_csv(GOLD_ATTRIBUTES_FIXTURE, index=False, compression="gzip")

    _require(GOLD_ANCESTORS_PATH, "run `python build_gold_set.py` first")
    ancestors = pd.read_parquet(GOLD_ANCESTORS_PATH)
    # Deliberately NOT sorted. features._wd_ancestor_names_by_rank() resolves an item's ancestor
    # at each target rank first-wins, so when a transitive P171 chain contains two ancestors at
    # the same rank (taxonomic disagreement in Wikidata — it happens), row order decides which
    # one is used. Sorting this fixture changed family_match/order_match on 4 of 2,610 rows and
    # moved rank:map's gold top-1 by half a point. Preserving the pull's own order keeps the
    # fixture path bit-for-bit identical to the full path, which is the only thing that makes it
    # a fixture rather than an approximation.
    ancestors.to_csv(GOLD_ANCESTORS_FIXTURE, index=False, compression="gzip")

    _require(DEFAULT_OOF_PATH, "run `python -m src.train` first")
    reference = oof_reference(pd.read_parquet(DEFAULT_OOF_PATH))
    OOF_SUMMARY_FIXTURE.write_text(json.dumps(reference, indent=2) + "\n")

    root = Path(__file__).resolve().parent
    for path, rows in [
        (GOLD_INAT_INDEX_FIXTURE, len(inat_index)),
        (GOLD_ATTRIBUTES_FIXTURE, len(attributes)),
        (GOLD_ANCESTORS_FIXTURE, len(ancestors)),
        (OOF_SUMMARY_FIXTURE, reference["n_rows"]),
    ]:
        size_kb = path.stat().st_size / 1024
        print(f"wrote {path.relative_to(root)}  ({rows:,} rows, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
