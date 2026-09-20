"""Score the checker's ambiguous link findings with the champion. Spec §7 milestone 16.

`wikidata-inat-checker` records the cases its own rules could not settle — a Wikidata taxon whose
name matches more than one plausible iNaturalist taxon — as `findings` rows with
`kind='link', status='ambiguous'`. That queue is this project's actual deployment distribution:
the gold set was drawn from it, and every number in the README describes how well the model ranks
exactly these.

**Read-only, and a ranking rather than a decision.** Reading the findings is in scope for this
milestone and writing anything back is not (`platform-design.md` §2.4, `future-work.md`) — the
direction of that integration is still open, and it should be the checker calling a service here
rather than this repo writing into its database. Nor does the output carry accept/reject flags:
`findings.md` §2 is the measurement that says the OOF-derived thresholds do **not** transfer to
this population, and shipping them anyway would hide the true match for a third of the queue.
What transfers is the ordering, so that is what gets written.

    .venv/bin/python -m src.ambiguous

Candidates are generated here rather than taken from the finding's payload, so the rows scored are
built exactly like the rows the model was trained on — same strategies, same K, same similarity.
The attribute and ancestor pulls reuse the gold-set code paths, which already exist for items
*without* P3151, with their own cache files.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from .paths import DATA_DIR, FINDINGS_DB_PATH

AMBIGUOUS_KIND = "link"
AMBIGUOUS_STATUS = "ambiguous"

DEFAULT_OUTPUT_PATH = DATA_DIR / "scored_ambiguous.parquet"
DEFAULT_ATTRIBUTES_PATH = DATA_DIR / "ambiguous_wikidata_attributes.parquet"
DEFAULT_ATTRIBUTES_MANIFEST_PATH = DATA_DIR / "ambiguous_wikidata_attributes.manifest.json"
DEFAULT_ANCESTORS_PATH = DATA_DIR / "ambiguous_wikidata_ancestors.parquet"
DEFAULT_ANCESTORS_MANIFEST_PATH = DATA_DIR / "ambiguous_wikidata_ancestors.manifest.json"

# The objective the README reports (milestone 15 promoted it over binary:logistic).
DEFAULT_OBJECTIVE = "rank"


DEFAULT_SNAPSHOT_PATH = DATA_DIR / "findings.snapshot.db"


def snapshot(db_path: Path = FINDINGS_DB_PATH, dest: Path = DEFAULT_SNAPSHOT_PATH) -> Path:
    """Copy the checker's database here, and read *that*.

    Two reasons, both learned by running this in a container rather than on the host:

    * **It is a WAL database, and reading one requires writing.** SQLite creates a `-shm`
      shared-memory file next to the database even for a pure read, so `mode=ro` against a
      read-only mount fails with `attempt to write a readonly database`. On the host it worked
      only because the checker's own directory happened to be writable.
    * **The `-wal` holds committed rows the `.db` does not.** Reading the main file alone —
      which is what mounting just `findings.db` would allow — silently returns a stale view.

    Copying both and opening the copy keeps this repo's "never writes to the checker" promise
    (§2.4) while still seeing everything committed. A copy taken while the checker is mid-write
    can be torn, so the result is integrity-checked below rather than trusted.
    """
    import shutil

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(db_path, dest)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        target = dest.with_name(dest.name + suffix)
        if side.exists():
            shutil.copyfile(side, target)
        elif target.exists():
            # A previous run's sidecar would otherwise be replayed into this run's copy.
            target.unlink()
    return dest


def load_findings(db_path: Path = FINDINGS_DB_PATH) -> pd.DataFrame:
    """The open ambiguous link findings, one row per Wikidata item, read from a local snapshot."""
    from .wikidata import TransientSourceError

    if not db_path.exists():
        raise SystemExit(
            f"{db_path} not found — this is the sibling checker's findings database.\n"
            "Set MATCHER_FINDINGS_DB, or run `npm run links` in wikidata-inat-checker."
        )

    conn = sqlite3.connect(snapshot(db_path))
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            # A snapshot taken mid-write. Transient by construction: the next attempt copies a
            # different moment, so this is worth an Airflow retry rather than a failed run.
            raise TransientSourceError(f"findings snapshot failed its integrity check: {check}")
        rows = conn.execute(
            """
            SELECT f.qid, t.taxon_name, t.rank, f.payload, f.discovered_at
            FROM findings AS f
            JOIN taxa AS t ON t.qid = f.qid
            WHERE f.kind = ? AND f.status = ?
            ORDER BY f.discovered_at, f.qid
            """,
            (AMBIGUOUS_KIND, AMBIGUOUS_STATUS),
        ).fetchall()
    finally:
        conn.close()

    records = []
    for qid, name, rank, payload, discovered_at in rows:
        candidates = (json.loads(payload) or {}).get("candidates") or [] if payload else []
        records.append({
            "wikidata_qid": qid,
            "wikidata_name": name,
            "wikidata_rank": rank,
            # What the checker itself offered, kept only as context for a human reading the
            # output: the rows actually scored are generated below.
            "checker_candidate_count": len(candidates),
            "discovered_at": discovered_at,
        })
    return pd.DataFrame.from_records(
        records,
        columns=["wikidata_qid", "wikidata_name", "wikidata_rank", "checker_candidate_count",
                 "discovered_at"],
    )


def build_candidate_rows(findings: pd.DataFrame, attributes: pd.DataFrame) -> pd.DataFrame:
    """A candidates.parquet-shaped frame for these items, from this project's own generation.

    Serial rather than pooled: the ambiguous queue is a few hundred items at most, where a process
    pool costs more in startup than it saves.
    """
    from .candidates import build_lookup_cache, generate_candidates

    by_qid = attributes.set_index("qid")
    conn = build_lookup_cache()
    try:
        rows = []
        for record in findings.itertuples():
            item = by_qid.loc[record.wikidata_qid] if record.wikidata_qid in by_qid.index else None
            name = (item["name"] if item is not None else None) or record.wikidata_name
            if not name:
                continue
            for candidate in generate_candidates(
                conn,
                name,
                synonym_names=list(item["synonym_names"]) if item is not None else None,
                basionym_names=list(item["basionym_names"]) if item is not None else None,
            ):
                # The same renaming candidates.py applies on its way into candidates.parquet:
                # generate_candidates() speaks the index's column names, build_features() expects
                # the cache's.
                rows.append({
                    "wikidata_qid": record.wikidata_qid,
                    "inat_taxon_id": candidate["taxon_id"],
                    "inat_name": candidate["name"],
                    "inat_rank": candidate["rank"],
                    "strategies": candidate["strategies"],
                    "similarity": candidate["similarity"],
                })
    finally:
        conn.close()
    return pd.DataFrame.from_records(rows)


def build_scoring_features(findings: pd.DataFrame) -> pd.DataFrame:
    """Features for the ambiguous items, computed the same way every other milestone computes
    them — the Wikidata attribute and ancestor pulls are the gold set's, which already handle
    items that have no P3151 at all."""
    from .features import _load_inat_index, build_features
    from .wikidata import build_ancestor_chains, build_gold_attribute_pull

    qids = findings["wikidata_qid"].tolist()
    attributes = build_gold_attribute_pull(
        qids, cache_path=DEFAULT_ATTRIBUTES_PATH, manifest_path=DEFAULT_ATTRIBUTES_MANIFEST_PATH
    )
    ancestors = build_ancestor_chains(
        qids, cache_path=DEFAULT_ANCESTORS_PATH, manifest_path=DEFAULT_ANCESTORS_MANIFEST_PATH
    )

    candidates = build_candidate_rows(findings, attributes)
    if candidates.empty:
        return candidates

    features = build_features(
        candidates[["wikidata_qid", "inat_taxon_id", "inat_name", "inat_rank", "strategies",
                    "similarity"]],
        attributes,
        ancestors,
        _load_inat_index(),
    )
    return features


def score(features: pd.DataFrame, objective: str = DEFAULT_OBJECTIVE) -> pd.DataFrame:
    """Rank each item's candidates with the champion. Raw score for the ordering — isotonic
    calibration's plateaus discard exactly the within-group ordering this output is (see
    `evaluate.ranking_score_column`) — and the calibrated probability alongside it, which is the
    only one of the two that means anything across items."""
    from .tracking import resolve_model

    model = resolve_model(objective)
    raw, calibrated = model.score(features)
    scored = features.copy()
    scored[f"{objective}_raw_score"] = raw
    scored[f"{objective}_calibrated_prob"] = calibrated
    scored["model_source"] = model.source
    scored["model_version"] = model.version or ""

    scored = scored.sort_values(["wikidata_qid", f"{objective}_raw_score"], ascending=[True, False])
    scored["rank_in_item"] = scored.groupby("wikidata_qid").cumcount() + 1
    return scored


OUTPUT_COLUMNS = [
    "wikidata_qid", "wikidata_name", "wikidata_rank", "inat_taxon_id", "inat_name", "inat_rank",
    "rank_in_item",
    "similarity", "rank_equal", "kingdom_match", "family_match", "order_match",
    "shared_ancestor_depth", "model_source", "model_version",
]


def write_output(scored: pd.DataFrame, findings: pd.DataFrame, objective: str = DEFAULT_OBJECTIVE,
                 output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    # The Wikidata name and rank come from the findings side: build_features() carries neither
    # (it works in QIDs and normalised parts), and an output a human is meant to read without
    # resolving QIDs by hand needs them.
    out = scored.merge(
        findings[["wikidata_qid", "wikidata_name", "wikidata_rank", "discovered_at"]],
        on="wikidata_qid", how="left",
    )
    columns = [c for c in OUTPUT_COLUMNS if c in out.columns]
    columns += [f"{objective}_raw_score", f"{objective}_calibrated_prob"]
    out = out[[*columns, "discovered_at"]]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(output_path, index=False)
    return output_path


def run(objective: str = DEFAULT_OBJECTIVE, output_path: Path = DEFAULT_OUTPUT_PATH) -> dict:
    findings = load_findings()
    if findings.empty:
        print("no open ambiguous link findings — nothing to score")
        return {"items": 0, "rows": 0, "output": None}

    print(f"{len(findings):,} ambiguous item(s) from the checker's findings database")
    features = build_scoring_features(findings)
    if features.empty:
        print("candidate generation returned nothing for these items")
        return {"items": len(findings), "rows": 0, "output": None}

    scored = score(features, objective)
    path = write_output(scored, findings, objective, output_path)
    print(f"{len(scored):,} candidate rows for {scored['wikidata_qid'].nunique():,} items -> {path}")
    print(scored.loc[scored["rank_in_item"] == 1,
                     ["wikidata_qid", "inat_taxon_id", "inat_name", f"{objective}_raw_score"]]
          .head(10).to_string(index=False))
    return {
        "items": int(scored["wikidata_qid"].nunique()),
        "rows": int(len(scored)),
        "output": str(path),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE, choices=("rank", "binary"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    run(args.objective, args.output)
