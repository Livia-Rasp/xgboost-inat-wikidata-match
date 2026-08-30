"""Metrics, plots, and gold-set scoring. See docs/inat-wikidata-match-spec.md §6.

Milestone 5 (spec §7) starts this file off with just the baseline: the honest exact-match rule,
tie-broken by iNat observation count, scored on the same GroupKFold folds features.py assigned.
The rest of spec §6's evaluation suite (review-queue reduction, MRR, gold set, error taxonomy,
stratified metrics) needs a trained model first and extends this file in later milestones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from .paths import DATA_DIR, GOLD_DIR
from .wikidata import HEADERS, RateLimiter, _fetch_with_retry, _qid_set_fingerprint

INATURALIST_API = "https://api.inaturalist.org/v1/taxa"
OBSERVATION_COUNT_BATCH_SIZE = 200

DEFAULT_OBS_COUNTS_PATH = DATA_DIR / "inat_observation_counts.parquet"
DEFAULT_OBS_COUNTS_MANIFEST_PATH = DATA_DIR / "inat_observation_counts.manifest.json"

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

    fixture = _observation_counts_fixture(taxon_ids)
    if fixture is not None:
        return fixture

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


def _observation_counts_fixture(taxon_ids: list[str]) -> pd.DataFrame | None:
    """The committed counts for the gold set's exact-match ties, when data/ has nothing.

    Without this, `make gold` is not offline. score_gold_set -> baseline_predict ->
    build_observation_counts hits api.inaturalist.org for the 480 taxon ids involved in a gold
    exact-match tie, because data/inat_observation_counts.parquet is gitignored and is not copied
    into the image. `.github/workflows/ci.yml` and README both claimed "no network" for that path
    and were wrong — and worse, the committed baseline number depended on a live API whose counts
    drift, so CI could fail without a commit.

    Only used when it covers every id asked for; a partial fixture would silently zero-fill the
    rest, and a zero observation count is a real tie-break value, not an absence.
    """
    from .fixtures import GOLD_OBS_COUNTS_FIXTURE, announce, read_csv_fixture

    if not GOLD_OBS_COUNTS_FIXTURE.exists():
        return None
    counts = read_csv_fixture(GOLD_OBS_COUNTS_FIXTURE, dtype={"taxon_id": str})
    if not set(taxon_ids).issubset(set(counts["taxon_id"])):
        return None
    announce("observation counts", GOLD_OBS_COUNTS_FIXTURE)
    return counts[counts["taxon_id"].isin(taxon_ids)].sort_values("taxon_id").reset_index(drop=True)


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
    group_sizes = exact.groupby("wikidata_qid")["inat_taxon_id"].transform("size")
    tied_ids = exact.loc[group_sizes > 1, "inat_taxon_id"].unique().tolist()

    obs_counts = (
        build_observation_counts(tied_ids)
        if tied_ids
        else pd.DataFrame(columns=["taxon_id", "observations_count"])
    )
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

GOLD_HARD_CASES_PATH = GOLD_DIR / "hard_cases.csv"
GOLD_ANCESTORS_PATH = DATA_DIR / "gold_wikidata_ancestors.parquet"


def _load_gold_attributes() -> pd.DataFrame:
    """The gold items' Wikidata attributes. The committed fixture drops `synonym_names` and
    `basionym_names`: those two feed candidate *generation* (build_gold_set.py), which the
    five-minute path does not run, and list-valued columns do not survive a CSV round-trip
    cleanly enough to be worth pretending otherwise."""
    from .wikidata import DEFAULT_GOLD_ATTRIBUTES_PATH

    if DEFAULT_GOLD_ATTRIBUTES_PATH.exists():
        return pd.read_parquet(DEFAULT_GOLD_ATTRIBUTES_PATH)

    from .fixtures import GOLD_ATTRIBUTES_FIXTURE, announce, read_csv_fixture

    announce("gold Wikidata attributes", GOLD_ATTRIBUTES_FIXTURE)
    return read_csv_fixture(GOLD_ATTRIBUTES_FIXTURE)


def _load_gold_ancestors() -> pd.DataFrame:
    if GOLD_ANCESTORS_PATH.exists():
        return pd.read_parquet(GOLD_ANCESTORS_PATH)

    from .fixtures import GOLD_ANCESTORS_FIXTURE, announce, read_csv_fixture

    announce("gold ancestor chains", GOLD_ANCESTORS_FIXTURE)
    return read_csv_fixture(GOLD_ANCESTORS_FIXTURE)


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

    wikidata_taxa = _load_gold_attributes()
    wikidata_taxa = wikidata_taxa[wikidata_taxa["qid"].isin(hard_cases["wikidata_qid"])].reset_index(drop=True)
    ancestors = _load_gold_ancestors()
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
    """Resolved through tracking.resolve_model(): the registry's champion when
    MLFLOW_TRACKING_URI is set, the committed data/models/ files otherwise — which is what keeps
    the five-minute path and CI's offline `make gold` working with no server."""
    from .tracking import resolve_model

    return resolve_model(objective).score(features)


def ranking_score_column(objective: str) -> str:
    """Within-group ranking (top-1 accuracy, MRR) must use the *raw* model score, not the
    calibrated probability.

    Isotonic calibration is a step function: it maps whole ranges of distinct raw scores onto a
    single output value ("plateaus"). That is exactly right for anything needing cross-group
    comparability — auto-accept/reject thresholds, Brier score — but it discards the relative
    ordering *inside* a candidate group, which is the only thing top-1/MRR measure. Ties then
    break on row order rather than on the model's actual preference.

    Negligible at OOF scale (~0.02pp over 590k rows, where plateaus rarely swallow a whole
    group), large on the gold set: `rank:map` read 43.5% top-1 on calibrated probabilities and
    95.65% on its own raw scores for the identical model and identical predictions."""
    return f"{objective}_raw_score"


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


BAND_LO, BAND_HI = 0.95, 1.01


def oof_reference(oof_predictions: pd.DataFrame, lo: float = BAND_LO, hi: float = BAND_HI) -> dict:
    """Everything gold-set scoring needs from the 12 MB OOF prediction table, reduced to a
    handful of numbers.

    Gold scoring never re-derives a threshold — that would be circular on a few hundred rows —
    it only re-applies the ones chosen on OOF data, and compares one score band. So the whole
    dependency is four floats and a count, which is why the five-minute path can ship them as a
    committed JSON file instead of the parquet they came from."""
    from .train import (
        find_auto_accept_threshold,
        find_reject_threshold,
        precision_at_threshold_table,
    )

    labels = oof_predictions["label"].to_numpy()
    mask = (oof_predictions["binary_raw_score"] >= lo) & (oof_predictions["binary_raw_score"] < hi)

    thresholds = {}
    for objective in ("binary", "rank"):
        probs = oof_predictions[f"{objective}_calibrated_prob"].to_numpy()
        accept_row = find_auto_accept_threshold(precision_at_threshold_table(probs, labels))
        thresholds[objective] = {
            "accept": float(accept_row["threshold"]) if accept_row is not None else None,
            "reject": find_reject_threshold(probs, labels),
        }

    return {
        "n_rows": int(len(oof_predictions)),
        "band": [lo, hi],
        "band_n": int(mask.sum()),
        "band_precision": float(labels[mask.to_numpy()].mean()) if mask.any() else float("nan"),
        "thresholds": thresholds,
    }


def load_oof_reference() -> dict:
    """The real OOF predictions when they exist, else the committed summary of them."""
    from .train import DEFAULT_OOF_PATH

    if DEFAULT_OOF_PATH.exists():
        return oof_reference(pd.read_parquet(DEFAULT_OOF_PATH))

    import json

    from .fixtures import OOF_SUMMARY_FIXTURE, announce

    if not OOF_SUMMARY_FIXTURE.exists():
        raise SystemExit(
            f"neither {DEFAULT_OOF_PATH} nor {OOF_SUMMARY_FIXTURE} exists — run "
            f"`python -m src.train` or `python build_fixtures.py`."
        )
    announce("OOF reference", OOF_SUMMARY_FIXTURE)
    return json.loads(OOF_SUMMARY_FIXTURE.read_text())


def gold_band_comparison(features: pd.DataFrame, reference: dict) -> dict:
    """The headline check: milestone 6 found raw binary:logistic scores >=0.95 sit at only 83.9%
    precision on OOF/P3151 data and hypothesized that gap was P3151 label noise, not a model
    gap. This is the direct test — same score band, but on hand-verified gold labels the model
    never saw and P3151 never touched. Higher gold precision in this band confirms it."""
    lo, hi = reference["band"]
    gold_mask = (features["binary_raw_score"] >= lo) & (features["binary_raw_score"] < hi)
    return {
        "band": f"[{lo}, {hi})",
        "oof_n": reference["band_n"],
        "oof_precision": reference["band_precision"],
        "gold_n": int(gold_mask.sum()),
        "gold_precision": float(features.loc[gold_mask, "label"].mean()) if gold_mask.any() else float("nan"),
    }


def gold_threshold_check(features: pd.DataFrame, reference: dict, objective: str) -> dict:
    """Re-applies the *exact* OOF-selected auto-accept threshold to gold data — deliberately not
    a freshly-swept threshold, which would be circular/noisy on only a few hundred gold rows.
    Reports whether that threshold, chosen without ever seeing gold data, still clears 99.5%
    precision independently."""
    from .train import AUTO_ACCEPT_PRECISION

    threshold = reference["thresholds"][objective]["accept"]
    if threshold is None:
        return {"objective": objective, "threshold": None, "gold_n": 0, "gold_precision": float("nan"), "holds": None}

    mask = features[f"{objective}_calibrated_prob"] >= threshold
    n = int(mask.sum())
    precision = float(features.loc[mask, "label"].mean()) if n else float("nan")
    return {
        "objective": objective,
        "threshold": threshold,
        "gold_n": n,
        "gold_precision": precision,
        "holds": (precision >= AUTO_ACCEPT_PRECISION) if n else None,
    }


def gold_top1_misses(features: pd.DataFrame, objective: str) -> pd.DataFrame:
    """Every item whose top-ranked candidate is not the labelled correct one, with the picked and
    the true candidate side by side. Spec §7 milestone 9 requires each of these to get a written
    characterization (close call / labeling error / model gap / other) rather than just being
    counted, so this returns the rows to review, not a number."""
    rank_col = ranking_score_column(objective)
    prob_col = f"{objective}_calibrated_prob"
    ranked = features.sort_values(["wikidata_qid", rank_col], ascending=[True, False])
    top = ranked.drop_duplicates("wikidata_qid", keep="first")
    missed_qids = top.loc[top["label"] != 1, "wikidata_qid"]
    # Items with no correct answer at all (labelled NONE) have no top-1 to get right; they are
    # scored by the abstention/reject path instead, not counted as ranking misses here.
    has_positive = features.groupby("wikidata_qid")["label"].max()
    missed_qids = [q for q in missed_qids if has_positive.get(q, 0) == 1]

    cols = ["wikidata_qid", "wikidata_name", "inat_taxon_id", "inat_name", "inat_rank", "label",
            "rank_equal", "kingdom_match", "family_match", "order_match", "shared_ancestor_depth",
            "similarity", "sim_margin_to_runner_up", "rank_trivial", rank_col, prob_col]
    cols = [c for c in cols if c in features.columns]
    out = features[features["wikidata_qid"].isin(missed_qids)][cols]
    return out.sort_values(["wikidata_qid", rank_col], ascending=[True, False])


def review_queue_reduction(features: pd.DataFrame, reference: dict, objective: str) -> dict:
    """How much human review the model removes, counted in *items* — the unit the review queue is
    actually measured in (`output/links-ambiguous.html` lists one row per ambiguous Wikidata
    item, not per candidate pair).

    Both thresholds come from the OOF data, never from the frame being scored, so this stays a
    genuine out-of-sample number when called on the gold set.

    Three item-level outcomes:
      auto_accept  the top-ranked candidate clears the 99.5%-precision accept threshold — write
                   it and move on.
      auto_reject  every candidate in the group sits below the reject threshold — no match
                   exists to write, so the item leaves the queue without a human opening it.
      review       everything else. Still human work, but the candidates that fall under the
                   reject threshold can be hidden, which is what `candidate_rows_ruled_out`
                   counts.
    """
    prob_col = f"{objective}_calibrated_prob"
    rank_col = ranking_score_column(objective)
    accept_threshold = reference["thresholds"][objective]["accept"]
    reject_threshold = reference["thresholds"][objective]["reject"]

    ranked = features.sort_values(["wikidata_qid", rank_col], ascending=[True, False])
    top = ranked.drop_duplicates("wikidata_qid", keep="first")

    per_item = pd.DataFrame({
        "wikidata_qid": top["wikidata_qid"].to_numpy(),
        "top_prob": top[prob_col].to_numpy(),
        "top_correct": (top["label"] == 1).to_numpy(),
    })
    group_max = features.groupby("wikidata_qid")[prob_col].max()
    group_has_positive = features.groupby("wikidata_qid")["label"].max()
    per_item["group_max_prob"] = per_item["wikidata_qid"].map(group_max).to_numpy()
    per_item["has_positive"] = per_item["wikidata_qid"].map(group_has_positive).to_numpy() == 1

    n_items = len(per_item)
    no_items = pd.Series(False, index=per_item.index)
    no_rows = pd.Series(False, index=features.index)

    accepted = (per_item["top_prob"] >= accept_threshold) if accept_threshold is not None else no_items
    below_reject = (per_item["group_max_prob"] < reject_threshold) if reject_threshold is not None else no_items
    rejected = below_reject & ~accepted

    # Row-level view of the reject threshold. Its OOF guarantee is that rows below it are negative
    # at least 99.5% of the time; whether that survives on a harder population has to be measured
    # rather than assumed, so it is reported here. `items_losing_true_match` is the cost of being
    # wrong about it: items whose real answer would be hidden from the reviewer.
    ruled_out = (features[prob_col] < reject_threshold) if reject_threshold is not None else no_rows
    n_rows_ruled_out = int(ruled_out.sum())
    lost = per_item["has_positive"] & below_reject

    def share(mask: pd.Series, values: pd.Series) -> float:
        return float(values[mask].mean()) if mask.any() else float("nan")

    return {
        "ruled_out_row_precision": share(ruled_out, features["label"] == 0),
        "items_losing_true_match": int(lost.sum()),
        "objective": objective,
        "n_items": n_items,
        "accept_threshold": accept_threshold,
        "reject_threshold": reject_threshold,
        "auto_accept_items": int(accepted.sum()),
        "auto_accept_precision": share(accepted, per_item["top_correct"]),
        "auto_reject_items": int(rejected.sum()),
        # A correct auto-reject means the item really has no match among its candidates.
        "auto_reject_precision": share(rejected, ~per_item["has_positive"]),
        "review_items": int(n_items - accepted.sum() - rejected.sum()),
        "items_removed_from_queue": float((accepted.sum() + rejected.sum()) / n_items) if n_items else float("nan"),
        "candidate_rows_ruled_out": float(n_rows_ruled_out / len(features)) if len(features) else float("nan"),
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
        top1, mrr = gold_top1_and_mrr(features, ranking_score_column(objective))
        metrics[objective] = {
            "top1_accuracy": top1,
            "mrr": mrr,
            # Brier stays on the calibrated probability — it scores probability quality, which is
            # what calibration is for, unlike the within-group ranking metrics above.
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


def gold_metrics(result: dict, reference: dict, objective: str) -> dict:
    """Every gold-set number, for one objective, in the same vocabulary train.oof_metrics() uses."""
    from .tracking import metric_key

    features = result["features"]
    variant = result["metrics"][objective]
    breakdown = gold_rank_trivial_breakdown(features, ranking_score_column(objective))
    check = gold_threshold_check(features, reference, objective)
    queue = review_queue_reduction(features, reference, objective)

    metrics = {
        metric_key("gold", objective, "top1", "raw"): variant["top1_accuracy"],
        metric_key("gold", objective, "mrr", "raw"): variant["mrr"],
        metric_key("gold", objective, "brier", "calibrated"): variant["brier"],
        metric_key("gold", objective, "accept_precision"): check["gold_precision"],
    }
    # `holds` is None and `gold_precision` NaN when no gold row clears the OOF accept threshold,
    # which is the actual result on this gold set rather than an edge case — zero rows clear it
    # for either objective. float(None) raised here and killed the whole logging call.
    if check["holds"] is not None:
        metrics[metric_key("gold", objective, "accept_threshold_holds")] = float(check["holds"])
    for bucket, values in breakdown.items():
        for name, value in values.items():
            metrics[metric_key("gold", objective, f"{bucket}.{name}")] = value
    # review_queue_reduction() carries its own 'objective' key, which is a string. Flattening the
    # dict wholesale fed that to log_metric and raised — after half the run's metrics were already
    # written, which is worse than not logging at all.
    for name, value in queue.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[metric_key("gold", objective, f"queue.{name}")] = value
    return metrics


def log_gold_metrics(result: dict, reference: dict) -> None:
    """Attach the gold numbers to the run that registered the model they were produced with.

    Gold scoring is a separate process from training, so the run id has to come from somewhere:
    it comes off the registry version (`resolve_model().run_id`), not from a file in data/ —
    an eighth cache manifest is the thing this milestone exists to remove.
    """
    from . import tracking

    if not tracking.enabled():
        return

    band = gold_band_comparison(result["features"], reference)
    shared = {
        tracking.metric_key("gold", "band", "n"): band["gold_n"],
        tracking.metric_key("gold", "band", "precision"): band["gold_precision"],
        tracking.metric_key("gold", "baseline", "top1"): result["metrics"]["baseline"]["top1_accuracy"],
        tracking.metric_key("gold", "recall_ceiling", "value"): result["recall_ceiling"],
        tracking.metric_key("gold", "items", "n"): result["features"]["wikidata_qid"].nunique(),
    }

    run_ids = {o: tracking.resolve_model(o).run_id for o in tracking.OBJECTIVES}
    if not any(run_ids.values()):
        print("\n(models came from data/models/, not the registry — gold metrics not logged)")
        return

    distinct = {rid for rid in run_ids.values() if rid}
    if len(distinct) > 1:
        # The two objectives were registered from different runs. Log each objective's own
        # metrics to its own run and tag the anomaly, rather than averaging it away.
        tracking.set_tags({"gold_eval_split": "true"})

    for objective, run_id in run_ids.items():
        if run_id is None:
            continue
        with tracking.resume(run_id):
            tracking.log_metrics(gold_metrics(result, reference, objective))
            if len(distinct) == 1:
                tracking.log_metrics(shared)
            tracking.set_tags({"gold_eval_at": datetime.now(UTC).isoformat(timespec="seconds")})
    print(f"\nlogged gold metrics to MLflow run(s): {', '.join(sorted(distinct))}")


if __name__ == "__main__":
    import argparse

    # argparse rather than `"--gold" in sys.argv`: three modules now take flags, and a
    # membership test accepts `--golf` silently and runs the wrong branch.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--gold",
        action="store_true",
        help="score the hand-labelled gold set instead of the baseline on the OOF population",
    )
    args = parser.parse_args()

    if args.gold:
        features = load_gold_features()
        result = score_gold_set(features)
        reference = load_oof_reference()

        print(f"Gold set: {features['wikidata_qid'].nunique():,} items, {len(features):,} candidate rows")
        print(f"Recall ceiling (found_by_generation rate): {result['recall_ceiling']:.2%}\n")

        print("Metrics by variant:")
        print(pd.DataFrame(result["metrics"]).T.to_string())

        print("\nRaw-score band comparison (the milestone 6 label-noise hypothesis test):")
        print(gold_band_comparison(result["features"], reference))

        print("\nAuto-accept threshold re-applied to gold (not re-swept):")
        for objective in ("binary", "rank"):
            print(f"  {objective}: {gold_threshold_check(result['features'], reference, objective)}")

        print("\nReview-queue reduction (item-level; thresholds taken from OOF, not re-swept here):")
        for objective in ("binary", "rank"):
            print(f"  {objective}: {review_queue_reduction(result['features'], reference, objective)}")

        n_trivial = result["features"].loc[result["features"]["rank_trivial"], "wikidata_qid"].nunique()
        print(f"\nTrivial-by-rank vs. other (rank alone disambiguates {n_trivial} of "
              f"{result['features']['wikidata_qid'].nunique()} items):")
        for objective in ("binary", "rank"):
            breakdown = gold_rank_trivial_breakdown(result["features"], ranking_score_column(objective))
            print(f"  {objective}: {breakdown}")

        print("\nTop-1 misses, for the milestone 9 per-miss review:")
        for objective in ("binary", "rank"):
            misses = gold_top1_misses(result["features"], objective)
            print(f"\n--- {objective}: {misses['wikidata_qid'].nunique()} item(s) ---")
            if not misses.empty:
                print(misses.to_string(index=False))

        log_gold_metrics(result, reference)
    else:
        from .features import DEFAULT_FEATURES_PATH

        features = pd.read_parquet(DEFAULT_FEATURES_PATH)
        predictions = baseline_predict(features)
        fold_scores, abstention_scores = score_baseline(features, predictions)

        print("Baseline (exact-match, observation-count tiebreak) — per fold and overall:")
        print(fold_scores.to_string())
        print("\nAbstention accuracy by reason:")
        print(abstention_scores.to_string())

        from .train import DEFAULT_OOF_PATH

        if DEFAULT_OOF_PATH.exists():
            oof = pd.read_parquet(DEFAULT_OOF_PATH)
            reference = oof_reference(oof)
            print("\nReview-queue reduction on the OOF population (item-level):")
            for objective in ("binary", "rank"):
                print(f"  {objective}: {review_queue_reduction(oof, reference, objective)}")
