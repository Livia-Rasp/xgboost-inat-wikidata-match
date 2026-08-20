"""Metrics, plots, and gold-set scoring. See docs/inat-wikidata-match-spec.md §6.

Milestone 5 (spec §7) starts this file off with just the baseline: the honest exact-match rule,
tie-broken by iNat observation count, scored on the same GroupKFold folds features.py assigned.
The rest of spec §6's evaluation suite (review-queue reduction, MRR, gold set, error taxonomy,
stratified metrics) needs a trained model first and extends this file in later milestones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

from .wikidata import HEADERS, RateLimiter, _fetch_with_retry, _qid_set_fingerprint

INATURALIST_API = "https://api.inaturalist.org/v1/taxa"
OBSERVATION_COUNT_BATCH_SIZE = 200

DEFAULT_OBS_COUNTS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "inat_observation_counts.parquet"
)
DEFAULT_OBS_COUNTS_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "inat_observation_counts.manifest.json"
)

_OBS_COUNT_RATE_LIMITER = RateLimiter(1.0)


def _chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fetch_observation_counts_batch(taxon_ids: list[str]) -> dict[str, int]:
    _OBS_COUNT_RATE_LIMITER.wait()
    resp = _fetch_with_retry(
        lambda: requests.get(
            INATURALIST_API,
            params={"id": ",".join(taxon_ids), "per_page": len(taxon_ids)},
            headers=HEADERS,
            timeout=30,
        ),
        retries=3,
        label="iNat API",
    )
    data = resp.json()
    return {str(r["id"]): r.get("observations_count", 0) for r in data.get("results", [])}


def build_observation_counts(
    taxon_ids: list[str],
    cache_path: Path = DEFAULT_OBS_COUNTS_PATH,
    manifest_path: Path = DEFAULT_OBS_COUNTS_MANIFEST_PATH,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """One row per taxon_id: observations_count, from the iNat API (not the 12.7 GB
    observations.csv.gz bulk dump — see the milestone 5 plan for why). Only ever called with the
    taxon_ids actually involved in an exact-match tie, not the full candidate set. Cached like
    the other pulls in this project — no time-based staleness, persists until the taxon_id set
    changes or force_refresh=True."""
    taxon_ids = sorted(set(taxon_ids))
    if not force_refresh and cache_path.exists() and _manifest_matches(manifest_path, taxon_ids):
        return pd.read_parquet(cache_path)

    counts: dict[str, int] = {}
    for batch in _chunked(taxon_ids, OBSERVATION_COUNT_BATCH_SIZE):
        counts.update(_fetch_observation_counts_batch(batch))

    df = pd.DataFrame(
        [{"taxon_id": tid, "observations_count": counts.get(tid, 0)} for tid in taxon_ids]
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    manifest_path.write_text(
        json.dumps(
            {"taxon_id_count": len(taxon_ids), "taxon_id_fingerprint": _qid_set_fingerprint(taxon_ids)},
            indent=2,
        )
    )
    return df


def _manifest_matches(manifest_path: Path, taxon_ids: list[str]) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("taxon_id_count") == len(taxon_ids) and manifest.get(
        "taxon_id_fingerprint"
    ) == _qid_set_fingerprint(taxon_ids)


def baseline_predict(features: pd.DataFrame) -> pd.DataFrame:
    """The honest baseline: exact normalised-name match, tie-broken by iNat observation count
    (deterministic secondary tiebreak on taxon_id, for reproducibility on the rare double-tie).
    Items with no exact match get no row (abstain). Returns one row per predicted wikidata_qid:
    wikidata_qid, predicted_taxon_id."""
    exact = features[features["strategy_exact"]].copy()
    tied_ids = exact.loc[exact.groupby("wikidata_qid")["inat_taxon_id"].transform("size") > 1, "inat_taxon_id"].unique().tolist()

    obs_counts = build_observation_counts(tied_ids) if tied_ids else pd.DataFrame(columns=["taxon_id", "observations_count"])
    count_by_taxon = obs_counts.set_index("taxon_id")["observations_count"].to_dict()
    exact["observations_count"] = exact["inat_taxon_id"].map(count_by_taxon).fillna(0)

    exact = exact.sort_values(
        ["wikidata_qid", "observations_count", "inat_taxon_id"],
        ascending=[True, False, True],
    )
    picked = exact.drop_duplicates("wikidata_qid", keep="first")
    return picked[["wikidata_qid", "inat_taxon_id"]].rename(columns={"inat_taxon_id": "predicted_taxon_id"})


def score_baseline(features: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    """Per-fold and overall baseline metrics: coverage (fraction with a prediction at all),
    precision (of predictions made, how often correct), accuracy (correct out of everything,
    including abstentions counted right only when no_answer_reason != 'has_positive'), and
    abstention correctness split by no_answer_reason (spec §6: evaluate abstention separately;
    stale_p3151 vs synthetic_dropout stay distinguishable per milestone 4)."""
    items = features.drop_duplicates("wikidata_qid")[
        ["wikidata_qid", "fold", "no_answer_reason"]
    ].merge(
        features.loc[features["label"] == 1, ["wikidata_qid", "inat_taxon_id"]].rename(
            columns={"inat_taxon_id": "true_taxon_id"}
        ),
        on="wikidata_qid",
        how="left",
    )
    items = items.merge(predictions, on="wikidata_qid", how="left")
    items["predicted"] = items["predicted_taxon_id"].notna()
    items["correct"] = (items["predicted_taxon_id"] == items["true_taxon_id"]) & items["true_taxon_id"].notna()
    items["correct_abstention"] = ~items["predicted"] & items["true_taxon_id"].isna()

    def summarize(group: pd.DataFrame) -> pd.Series:
        n = len(group)
        n_predicted = group["predicted"].sum()
        return pd.Series(
            {
                "n_items": n,
                "coverage": n_predicted / n if n else 0.0,
                "precision": group.loc[group["predicted"], "correct"].mean() if n_predicted else float("nan"),
                "accuracy": (group["correct"] | group["correct_abstention"]).mean() if n else 0.0,
            }
        )

    per_fold = items.groupby("fold").apply(summarize, include_groups=False)
    overall = summarize(items)
    overall.name = "overall"
    result = pd.concat([per_fold, overall.to_frame().T])

    abstention = (
        items[items["true_taxon_id"].isna()]
        .groupby("no_answer_reason")["correct_abstention"]
        .agg(["mean", "count"])
        .rename(columns={"mean": "abstention_accuracy", "count": "n"})
    )
    return result, abstention


if __name__ == "__main__":
    from .features import DEFAULT_FEATURES_PATH

    features = pd.read_parquet(DEFAULT_FEATURES_PATH)
    predictions = baseline_predict(features)
    fold_scores, abstention_scores = score_baseline(features, predictions)

    print("Baseline (exact-match, observation-count tiebreak) — per fold and overall:")
    print(fold_scores.to_string())
    print("\nAbstention accuracy by reason:")
    print(abstention_scores.to_string())
