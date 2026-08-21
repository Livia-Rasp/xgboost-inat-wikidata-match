"""Metrics, plots, and gold-set scoring. See docs/inat-wikidata-match-spec.md §6.

Milestone 5 (spec §7) starts this file off with just the baseline: the honest exact-match rule,
tie-broken by iNat observation count, scored on the same GroupKFold folds features.py assigned.
The rest of spec §6's evaluation suite (review-queue reduction, MRR, gold set, error taxonomy,
stratified metrics) needs a trained model first and extends this file in later milestones.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xgboost

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


# ---- Gold set (milestone 7, spec §6 point 6) -------------------------------------------------
#
# The gold set is items P3151 never touched (that's the whole point — the only evaluation not
# contaminated by bot-added labels), so it can't be looked up in data/wikidata_taxa.parquet /
# data/candidates.parquet / data/features.parquet at all. build_gold_set.py builds the parallel
# small-scale inputs (gold/hard_cases.csv + the cached data/gold_wikidata_*.parquet pulls) this
# section reads. See gold/README.md for the full generate → label → evaluate workflow.

GOLD_DIR = Path(__file__).resolve().parent.parent / "gold"
GOLD_HARD_CASES_PATH = GOLD_DIR / "hard_cases.csv"
GOLD_ANCESTORS_PATH = Path(__file__).resolve().parent.parent / "data" / "gold_wikidata_ancestors.parquet"


def load_gold_features() -> pd.DataFrame:
    """gold/hard_cases.csv already carries every column build_features() needs from a
    candidates.parquet-shaped frame (build_gold_set.py wrote it that way on purpose) — this just
    re-attaches the Wikidata attributes/ancestors and runs the same feature computation every
    other milestone uses, so gold-set features are computed identically to training-set ones."""
    from .features import _load_inat_index, build_features

    if not GOLD_HARD_CASES_PATH.exists():
        raise SystemExit(f"{GOLD_HARD_CASES_PATH} not found — run build_gold_set.py first.")

    # inat_taxon_id must stay str to match inat_index/candidates.parquet's convention (taxon_id
    # is a SQLite TEXT column) — an all-numeric-looking CSV column would otherwise infer int64
    # and break the later merge in build_features().
    hard_cases = pd.read_csv(GOLD_HARD_CASES_PATH, dtype={"inat_taxon_id": str})
    from .wikidata import DEFAULT_GOLD_ATTRIBUTES_PATH

    wikidata_taxa = pd.read_parquet(DEFAULT_GOLD_ATTRIBUTES_PATH)
    wikidata_taxa = wikidata_taxa[wikidata_taxa["qid"].isin(hard_cases["wikidata_qid"])].reset_index(drop=True)
    ancestors = pd.read_parquet(GOLD_ANCESTORS_PATH)
    inat_index = _load_inat_index()

    candidates = hard_cases[
        ["wikidata_qid", "inat_taxon_id", "inat_name", "inat_rank", "strategies", "similarity"]
    ].copy()
    candidates["strategies"] = candidates["strategies"].fillna("")

    features = build_features(candidates, wikidata_taxa, ancestors, inat_index)
    features["label"] = hard_cases["label"].to_numpy()
    features["found_by_generation"] = hard_cases["found_by_generation"].to_numpy()
    features["notes"] = hard_cases["notes"].fillna("").to_numpy()

    # "Trivial by rank": exactly one candidate in the group has the WD item's own stated rank
    # (e.g. a species complex/section sharing a name string with its representative species,
    # where only the species-ranked candidate matches P105) — disambiguable by rank_equal alone,
    # no similarity judgment needed. A cheap sanity floor: if the model misses these, something
    # more basic than the hard cases is broken.
    rank_equal_counts = features.groupby("wikidata_qid")["rank_equal"].transform("sum")
    group_sizes = features.groupby("wikidata_qid")["wikidata_qid"].transform("size")
    features["rank_trivial"] = (rank_equal_counts == 1) & (group_sizes >= 2)
    return features


def score_gold_with_model(features: pd.DataFrame, objective: str) -> tuple[np.ndarray, np.ndarray]:
    from .train import DEFAULT_MODEL_DIR, _prepare_X

    model = xgboost.XGBClassifier() if objective == "binary" else xgboost.XGBRanker()
    model.load_model(DEFAULT_MODEL_DIR / f"{objective}_model.json")
    calibrator = pickle.loads((DEFAULT_MODEL_DIR / f"{objective}_calibrator.pkl").read_bytes())

    X = _prepare_X(features)
    raw = model.predict_proba(X)[:, 1] if objective == "binary" else model.predict(X)
    return raw, calibrator.predict(raw)


def gold_top1_and_mrr(features: pd.DataFrame, score_col: str) -> tuple[float, float]:
    from .train import top1_accuracy_and_mrr

    return top1_accuracy_and_mrr(features[["wikidata_qid", "label", score_col]], score_col)


def gold_rank_trivial_breakdown(features: pd.DataFrame, score_col: str) -> dict:
    """Splits top-1 accuracy/MRR into 'trivial by rank' groups (see load_gold_features) vs
    everything else. Trivial-group accuracy is a sanity floor, not a headline number — a real
    model should be at ~100% there; the genuinely informative comparison is how much accuracy
    drops on the non-trivial remainder, which is where the model's actual judgment is tested."""
    trivial_qids = features.loc[features["rank_trivial"], "wikidata_qid"].unique()
    trivial = features[features["wikidata_qid"].isin(trivial_qids)]
    other = features[~features["wikidata_qid"].isin(trivial_qids)]
    result = {}
    for name, subset in [("trivial_by_rank", trivial), ("other", other)]:
        if subset.empty:
            result[name] = {"n_items": 0, "top1_accuracy": float("nan"), "mrr": float("nan")}
            continue
        top1, mrr = gold_top1_and_mrr(subset, score_col)
        result[name] = {"n_items": subset["wikidata_qid"].nunique(), "top1_accuracy": top1, "mrr": mrr}
    return result


def gold_band_comparison(
    features: pd.DataFrame, oof_predictions: pd.DataFrame, lo: float = 0.95, hi: float = 1.01
) -> dict:
    """The headline check: milestone 6 found raw binary:logistic scores >=0.95 sit at only 83.9%
    precision on OOF/P3151 data and hypothesized that gap was P3151 label noise, not a model
    gap. This is the direct test — same score band, but on hand-verified gold labels the model
    never saw and P3151 never touched. Higher gold precision in this band confirms it."""
    oof_mask = (oof_predictions["binary_raw_score"] >= lo) & (oof_predictions["binary_raw_score"] < hi)
    gold_mask = (features["binary_raw_score"] >= lo) & (features["binary_raw_score"] < hi)
    return {
        "band": f"[{lo}, {hi})",
        "oof_n": int(oof_mask.sum()),
        "oof_precision": float(oof_predictions.loc[oof_mask, "label"].mean()) if oof_mask.any() else float("nan"),
        "gold_n": int(gold_mask.sum()),
        "gold_precision": float(features.loc[gold_mask, "label"].mean()) if gold_mask.any() else float("nan"),
    }


def gold_threshold_check(features: pd.DataFrame, oof_predictions: pd.DataFrame, objective: str) -> dict:
    """Re-applies the *exact* OOF-selected auto-accept threshold to gold data — deliberately not
    a freshly-swept threshold, which would be circular/noisy on only ~300-500 gold rows. Reports
    whether that threshold, chosen without ever seeing gold data, still clears 99.5% precision
    independently."""
    from .train import AUTO_ACCEPT_PRECISION, find_auto_accept_threshold, precision_at_threshold_table

    oof_col = f"{objective}_calibrated_prob"
    oof_table = precision_at_threshold_table(oof_predictions[oof_col].to_numpy(), oof_predictions["label"].to_numpy())
    oof_threshold_row = find_auto_accept_threshold(oof_table)
    if oof_threshold_row is None:
        return {"objective": objective, "threshold": None, "gold_n": 0, "gold_precision": float("nan"), "holds": None}

    threshold = oof_threshold_row["threshold"]
    gold_col = f"{objective}_calibrated_prob"
    mask = features[gold_col] >= threshold
    n = int(mask.sum())
    precision = float(features.loc[mask, "label"].mean()) if n else float("nan")
    return {
        "objective": objective,
        "threshold": threshold,
        "gold_n": n,
        "gold_precision": precision,
        "holds": (precision >= AUTO_ACCEPT_PRECISION) if n else None,
    }


def score_gold_set(features: pd.DataFrame) -> dict:
    """Full gold-set scoring: both model variants + baseline. Mutates and returns `features`
    with score columns attached (so callers can reuse it for plots/error taxonomy) alongside a
    metrics dict."""
    labels = features["label"].to_numpy()
    metrics = {}

    for objective in ("binary", "rank"):
        raw, calibrated = score_gold_with_model(features, objective)
        features[f"{objective}_raw_score"] = raw
        features[f"{objective}_calibrated_prob"] = calibrated
        top1, mrr = gold_top1_and_mrr(features, f"{objective}_calibrated_prob")
        metrics[objective] = {
            "top1_accuracy": top1,
            "mrr": mrr,
            "brier": float(np.mean((calibrated - labels) ** 2)),
        }

    baseline_predictions = baseline_predict(features)
    items = features.drop_duplicates("wikidata_qid")[["wikidata_qid"]].merge(
        features.loc[features["label"] == 1, ["wikidata_qid", "inat_taxon_id"]].rename(
            columns={"inat_taxon_id": "true_taxon_id"}
        ),
        on="wikidata_qid",
        how="left",
    ).merge(baseline_predictions, on="wikidata_qid", how="left")
    items["correct"] = (items["predicted_taxon_id"] == items["true_taxon_id"]) & items["true_taxon_id"].notna()
    items["abstained_correctly"] = items["predicted_taxon_id"].isna() & items["true_taxon_id"].isna()
    metrics["baseline"] = {
        "top1_accuracy": float((items["correct"] | items["abstained_correctly"]).mean()),
        "mrr": None,
        "brier": None,
    }

    # Per-item recall: among items with a genuine correct answer (label==1 somewhere in the
    # group — NONE-answer items have no true positive and aren't counted), was that true
    # candidate found by generation? drop_duplicates("wikidata_qid") on the whole frame picks an
    # arbitrary row per item — for a group with one True and nine False `found_by_generation`
    # rows (the common case, only the true positive gets flagged False on a miss), it can easily
    # land on a True row and mask a real miss. Filtering to label==1 rows first fixes that.
    positives = features[features["label"] == 1]
    recall_ceiling = float(positives["found_by_generation"].mean()) if len(positives) else float("nan")
    return {"features": features, "metrics": metrics, "recall_ceiling": recall_ceiling}


if __name__ == "__main__":
    import sys

    if "--gold" in sys.argv:
        from .train import DEFAULT_OOF_PATH

        features = load_gold_features()
        result = score_gold_set(features)
        oof = pd.read_parquet(DEFAULT_OOF_PATH)

        print(f"Gold set: {features['wikidata_qid'].nunique():,} items, {len(features):,} candidate rows")
        print(f"Recall ceiling (found_by_generation rate): {result['recall_ceiling']:.2%}\n")

        print("Metrics by variant:")
        print(pd.DataFrame(result["metrics"]).T.to_string())

        print("\nRaw-score band comparison (the milestone 6 label-noise hypothesis test):")
        print(gold_band_comparison(result["features"], oof))

        print("\nAuto-accept threshold re-applied to gold (not re-swept):")
        for objective in ("binary", "rank"):
            print(f"  {objective}: {gold_threshold_check(result['features'], oof, objective)}")

        n_trivial = result["features"].loc[result["features"]["rank_trivial"], "wikidata_qid"].nunique()
        print(f"\nTrivial-by-rank vs. other (rank alone disambiguates {n_trivial} of "
              f"{result['features']['wikidata_qid'].nunique()} items):")
        for objective in ("binary", "rank"):
            breakdown = gold_rank_trivial_breakdown(result["features"], f"{objective}_calibrated_prob")
            print(f"  {objective}: {breakdown}")
    else:
        from .features import DEFAULT_FEATURES_PATH

        features = pd.read_parquet(DEFAULT_FEATURES_PATH)
        predictions = baseline_predict(features)
        fold_scores, abstention_scores = score_baseline(features, predictions)

        print("Baseline (exact-match, observation-count tiebreak) — per fold and overall:")
        print(fold_scores.to_string())
        print("\nAbstention accuracy by reason:")
        print(abstention_scores.to_string())
