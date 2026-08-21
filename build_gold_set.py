"""Turn a filled-in gold/labeling_filled.csv into the committed gold/hard_cases.csv, ready for
`python -m src.evaluate --gold` to score. See gold/README.md for the full workflow.

Lives at the repo root, alongside build_gold_labeling_kit.py — same reasoning: one-off tooling
that drives the src/ pipeline for a specific task, not part of the pipeline itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from src.candidates import DEFAULT_CACHE_PATH as LOOKUP_SQLITE_PATH
from src.candidates import build_lookup_cache, generate_candidates
from src.wikidata import (
    DEFAULT_GOLD_ATTRIBUTES_PATH,
    build_ancestor_chains,
    build_gold_attribute_pull,
)

GOLD_DIR = Path(__file__).resolve().parent / "gold"
FILLED_CSV_PATH = GOLD_DIR / "labeling_filled.csv"
HARD_CASES_PATH = GOLD_DIR / "hard_cases.csv"

DATA_DIR = Path(__file__).resolve().parent / "data"
GOLD_ANCESTORS_PATH = DATA_DIR / "gold_wikidata_ancestors.parquet"
GOLD_ANCESTORS_MANIFEST_PATH = DATA_DIR / "gold_wikidata_ancestors.manifest.json"

HARD_CASES_COLUMNS = [
    "wikidata_qid",
    "wikidata_name",
    "inat_taxon_id",
    "inat_name",
    "inat_rank",
    "strategies",
    "similarity",
    "label",
    "found_by_generation",
    "notes",
]


def load_answers(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"{path} not found — see gold/README.md: label gold/labeling_template.csv and "
            f"save it as {path.name}."
        )
    # keep_default_na=False: pandas' default NA-token list includes "None"/"NA"/"NULL" etc, so a
    # labeler writing "None" instead of the documented "NONE" would otherwise get silently
    # blanked here and the row dropped entirely instead of read as a no-match answer (caught on
    # a real run: Q111270149's "None" answer vanished this way).
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df[df["correct_inat_taxon_id"].str.strip() != ""].reset_index(drop=True)
    return df


def main() -> None:
    answers = load_answers(FILLED_CSV_PATH)
    print(f"{len(answers):,} answered rows in {FILLED_CSV_PATH.name}")
    if len(answers) < 300:
        print(f"NOTE: fewer than 300 answers ({len(answers)}) — spec targets ~200+, you're still covered, but more helps.")

    qids = answers["wikidata_qid"].tolist()
    wikidata_taxa = build_gold_attribute_pull(qids, cache_path=DEFAULT_GOLD_ATTRIBUTES_PATH)
    build_ancestor_chains(qids, cache_path=GOLD_ANCESTORS_PATH, manifest_path=GOLD_ANCESTORS_MANIFEST_PATH)

    conn = build_lookup_cache()

    rows: list[dict] = []
    n_found = 0
    n_with_answer = 0
    for _, ans in answers.iterrows():
        qid = ans["wikidata_qid"]
        wd_match = wikidata_taxa[wikidata_taxa["qid"] == qid]
        if wd_match.empty:
            print(f"WARNING: no Wikidata data pulled for {qid} — skipping")
            continue
        wd_row = wd_match.iloc[0]
        syn = list(wd_row["synonym_names"]) if wd_row["synonym_names"] is not None else []
        bas = list(wd_row["basionym_names"]) if wd_row["basionym_names"] is not None else []
        candidates = generate_candidates(conn, wd_row["name"], syn, bas)
        found_ids = {c["taxon_id"] for c in candidates}

        correct_id = ans["correct_inat_taxon_id"].strip()
        is_none = correct_id.upper() == "NONE"
        if not is_none:
            n_with_answer += 1

        for c in candidates:
            label = 0 if is_none else int(c["taxon_id"] == correct_id)
            rows.append(
                {
                    "wikidata_qid": qid,
                    "wikidata_name": wd_row["name"],
                    "inat_taxon_id": c["taxon_id"],
                    "inat_name": c["name"],
                    "inat_rank": c["rank"],
                    "strategies": c["strategies"],
                    "similarity": c["similarity"],
                    "label": label,
                    "found_by_generation": True,
                    "notes": ans.get("notes", ""),
                }
            )

        if not is_none:
            if correct_id in found_ids:
                n_found += 1
            else:
                inat_row = conn.execute(
                    "SELECT name, rank FROM taxa_normalized WHERE taxon_id=?", (correct_id,)
                ).fetchone()
                rows.append(
                    {
                        "wikidata_qid": qid,
                        "wikidata_name": wd_row["name"],
                        "inat_taxon_id": correct_id,
                        "inat_name": inat_row[0] if inat_row else "?",
                        "inat_rank": inat_row[1] if inat_row else "?",
                        "strategies": "",
                        "similarity": None,
                        "label": 1,
                        "found_by_generation": False,
                        "notes": ans.get("notes", ""),
                    }
                )

    out = pd.DataFrame(rows, columns=HARD_CASES_COLUMNS)
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(HARD_CASES_PATH, index=False)
    print(f"\nwrote {len(out):,} rows ({out['wikidata_qid'].nunique():,} items) to {HARD_CASES_PATH}")
    if n_with_answer:
        print(
            f"recall on gold sample (found_by_generation among non-NONE answers): "
            f"{n_found}/{n_with_answer} = {n_found/n_with_answer:.2%}"
        )


if __name__ == "__main__":
    main()
