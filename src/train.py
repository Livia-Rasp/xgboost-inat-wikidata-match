"""XGBoost training, cross-validation, calibration, and threshold selection.
See docs/inat-wikidata-match-spec.md §5 and §7 milestone 6.

Two objective variants are compared (spec: "report both; whichever wins, the comparison is
worth a paragraph") — binary:logistic and rank:map (XGBoost's current docs recommend rank:map
over the literally-spec'd rank:pairwise for binary-relevance labels like ours; see the milestone
6 plan). Neither is picked as "the" model yet — that's milestone 7's job, once the gold set
(uncontaminated by bot-added P3151 labels) is available to break the tie honestly.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost
from sklearn.isotonic import IsotonicRegression

FEATURE_COLUMNS = [
    "similarity",
    "name_exact_raw", "name_exact_norm", "jaro_winkler_full", "levenshtein_ratio_full",
    "genus_exact", "genus_jw", "epithet_exact", "epithet_jw", "epithet_stem_match",
    "token_count_diff", "length_diff", "infra_rank_match", "infra_epithet_match", "hybrid_flag_match",
    "rank_equal", "rank_level_diff", "kingdom_match", "family_match", "order_match",
    "shared_ancestor_depth", "parent_name_jw",
    "n_candidates", "n_inat_taxa_same_name", "n_wikidata_items_same_name",
    "sim_rank_in_group", "sim_margin_to_runner_up",
    "strategy_basionym_epithet_genus_fuzzy", "strategy_basionym_exact", "strategy_basionym_genus_epithet_fuzzy",
    "strategy_epithet_genus_fuzzy", "strategy_exact", "strategy_genus_epithet_fuzzy",
    "strategy_synonym_epithet_genus_fuzzy", "strategy_synonym_exact", "strategy_synonym_genus_epithet_fuzzy",
    "strategy_trigram",
    "wikidata_sitelink_count", "wikidata_statement_count", "wikidata_has_iucn", "wikidata_has_commons_cat",
]

# Spec §5's "nice touch": monotone increasing on these three — costs a little accuracy, buys
# defensibility (a candidate can never look *less* plausible for scoring higher on any of them).
MONOTONE_UP = {"jaro_winkler_full", "shared_ancestor_depth", "kingdom_match"}

RANDOM_STATE = 42

TREE_PARAMS = dict(
    tree_method="hist",
    n_estimators=2000,
    learning_rate=0.05,
    max_depth=6,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    early_stopping_rounds=50,
    random_state=RANDOM_STATE,
)

AUTO_ACCEPT_PRECISION = 0.995
REJECT_NEG_PRECISION = 0.995

DEFAULT_OOF_PATH = Path(__file__).resolve().parent.parent / "data" / "oof_predictions.parquet"
DEFAULT_OOF_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "oof_predictions.manifest.json"
)
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "models"


def monotone_constraints_tuple(feature_columns: list[str] = FEATURE_COLUMNS) -> tuple[int, ...]:
    return tuple(1 if f in MONOTONE_UP else 0 for f in feature_columns)


def _prepare_X(df: pd.DataFrame) -> pd.DataFrame:
    X = df[FEATURE_COLUMNS].copy()
    for col in X.columns:
        if X[col].dtype == bool:
            X[col] = X[col].astype("int8")
    return X


def _fold_plan(folds: list[int]) -> dict[int, dict]:
    """For each fold as held-out test: train on the other folds minus one, which is held out as
    the early-stopping validation set. Deterministic (sorted), not random, so this is
    reproducible across runs without needing to seed anything here."""
    plan = {}
    for test_fold in folds:
        remaining = [f for f in folds if f != test_fold]
        val_fold = remaining[0]
        train_folds = remaining[1:]
        plan[test_fold] = {"train": train_folds, "val": val_fold}
    return plan


def train_oof(df: pd.DataFrame, objective: str) -> tuple[np.ndarray, list]:
    """5-fold out-of-fold predictions for one objective variant ('binary' or 'rank').
    df must already be sorted by wikidata_qid (caller's job, done once for both variants)."""
    folds = sorted(df["fold"].unique())
    plan = _fold_plan(folds)
    monotone = monotone_constraints_tuple()
    if objective == "rank":
        df = df.copy()
        df["_qid_int"] = pd.factorize(df["wikidata_qid"])[0]

    oof_scores = np.full(len(df), np.nan)
    models = []
    for test_fold, split in plan.items():
        train_mask = df["fold"].isin(split["train"])
        val_mask = df["fold"] == split["val"]
        test_mask = df["fold"] == test_fold

        X_train, y_train = _prepare_X(df[train_mask]), df.loc[train_mask, "label"]
        X_val, y_val = _prepare_X(df[val_mask]), df.loc[val_mask, "label"]
        X_test = _prepare_X(df[test_mask])

        if objective == "binary":
            scale_pos_weight = (y_train == 0).sum() / (y_train == 1).sum()
            model = xgboost.XGBClassifier(
                **TREE_PARAMS,
                objective="binary:logistic",
                eval_metric="logloss",
                scale_pos_weight=scale_pos_weight,
                monotone_constraints=monotone,
            )
            model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
            scores = model.predict_proba(X_test)[:, 1]
        elif objective == "rank":
            # XGBRanker's qid must be numeric (cast to uint32 internally) — wikidata_qid is a
            # string like "Q1023523", so factorize to dense integer group ids first. Global
            # factorization (not per-split) just needs to preserve "same qid -> same group id",
            # which it does regardless of scope.
            qid_train = df.loc[train_mask, "_qid_int"]
            qid_val = df.loc[val_mask, "_qid_int"]
            model = xgboost.XGBRanker(
                **TREE_PARAMS,
                objective="rank:map",
                eval_metric="map",
                monotone_constraints=monotone,
            )
            model.fit(
                X_train, y_train, qid=qid_train,
                eval_set=[(X_val, y_val)], eval_qid=[qid_val], verbose=False,
            )
            scores = model.predict(X_test)
        else:
            raise ValueError(objective)

        oof_scores[test_mask.values] = scores
        models.append(model)

    return oof_scores, models


def train_final_model(df: pd.DataFrame, objective: str, n_estimators: int):
    """Refit on all folds, for milestone 7's gold-set scoring. No held-out eval_set exists at
    this point, so there's nothing for early stopping to watch — n_estimators is fixed instead,
    to the OOF fold models' best_iteration values averaged (caller's job to compute, from the
    models train_oof() returned), as a reasonable stand-in."""
    X, y = _prepare_X(df), df["label"]
    monotone = monotone_constraints_tuple()
    params = dict(TREE_PARAMS)
    params.pop("early_stopping_rounds")
    params["n_estimators"] = n_estimators

    if objective == "binary":
        scale_pos_weight = (y == 0).sum() / (y == 1).sum()
        model = xgboost.XGBClassifier(
            **params, objective="binary:logistic", eval_metric="logloss",
            scale_pos_weight=scale_pos_weight, monotone_constraints=monotone,
        )
        model.fit(X, y, verbose=False)
    elif objective == "rank":
        qid = pd.factorize(df["wikidata_qid"])[0]
        model = xgboost.XGBRanker(
            **params, objective="rank:map", eval_metric="map", monotone_constraints=monotone,
        )
        model.fit(X, y, qid=qid, verbose=False)
    else:
        raise ValueError(objective)
    return model


def calibrate(scores: np.ndarray, labels: np.ndarray) -> tuple[IsotonicRegression, np.ndarray]:
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(scores, labels)
    return iso, iso.predict(scores)


def precision_at_threshold_table(probs: np.ndarray, labels: np.ndarray, n_steps: int = 101) -> pd.DataFrame:
    thresholds = np.linspace(0, 1, n_steps)
    rows = []
    for t in thresholds:
        mask = probs >= t
        n = int(mask.sum())
        rows.append(
            {
                "threshold": round(float(t), 6),  # np.linspace float64 noise, e.g. 0.8200000000000001
                "n": n,
                "coverage": n / len(labels),
                "precision": float(labels[mask].mean()) if n else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def find_auto_accept_threshold(table: pd.DataFrame, target_precision: float = AUTO_ACCEPT_PRECISION):
    eligible = table[table["precision"] >= target_precision].sort_values("threshold")
    return eligible.iloc[0] if not eligible.empty else None


def find_reject_threshold(probs: np.ndarray, labels: np.ndarray, target_neg_precision: float = REJECT_NEG_PRECISION, n_steps: int = 101) -> float | None:
    """Highest threshold t such that every row scoring below t is negative at least
    target_neg_precision of the time — the symmetric, reject-side counterpart to the auto-accept
    threshold above."""
    best = None
    for t in np.linspace(0, 1, n_steps):
        mask = probs < t
        n = int(mask.sum())
        if n == 0:
            continue
        neg_precision = float((labels[mask] == 0).mean())
        if neg_precision >= target_neg_precision:
            best = t
    # np.linspace produces float64 noise (e.g. 0.8200000000000001) — round to the step's own
    # precision rather than print that noise in every report.
    return round(float(best), 6) if best is not None else None


def top1_accuracy_and_mrr(df: pd.DataFrame, prob_col: str) -> tuple[float, float]:
    """Group-level: did the highest-probability candidate match the true positive, and at what
    reciprocal rank did the true positive land.

    Vectorized rather than groupby().apply() with a per-group Python function — the latter is
    fine at small scale (that's how the milestone 6 smoke test first validated this function) but
    took minutes at the full 590k-row/58,842-group scale, dominating what should have been a
    sub-second cache-hit rerun. Sorting once and using cumcount() for within-group rank does the
    same computation in one vectorized pass."""
    ranked = df.sort_values(["wikidata_qid", prob_col], ascending=[True, False])
    rank = ranked.groupby("wikidata_qid").cumcount().to_numpy() + 1
    is_true = ranked["label"].to_numpy() == 1
    true_ranks = rank[is_true]
    return float((true_ranks == 1).mean()), float((1.0 / true_ranks).mean())


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    return float(np.mean((probs - labels) ** 2))


def reliability_diagram_data(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    bins = np.linspace(0, 1, n_bins + 1)
    bin_idx = np.digitize(probs, bins[1:-1])
    rows = []
    for b in range(n_bins):
        mask = bin_idx == b
        if mask.sum() == 0:
            continue
        rows.append(
            {
                "bin_mid": (bins[b] + bins[b + 1]) / 2,
                "mean_predicted": float(probs[mask].mean()),
                "mean_observed": float(labels[mask].mean()),
                "n": int(mask.sum()),
            }
        )
    return pd.DataFrame(rows)


def _oof_manifest_matches(manifest_path: Path, shape_key: dict) -> bool:
    """Subset match, not exact dict equality — the manifest gets extra keys
    (*_avg_best_iteration) written after training completes, which shape_key (built before
    training, for the cache-hit check) never has. Exact equality would never match, silently
    forcing a full retrain on every single call regardless of force_refresh."""
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return all(manifest.get(k) == v for k, v in shape_key.items())


def _avg_best_iteration(models: list) -> int:
    iterations = [getattr(m, "best_iteration", None) for m in models]
    iterations = [i for i in iterations if i is not None]
    return int(round(sum(iterations) / len(iterations))) if iterations else TREE_PARAMS["n_estimators"]


def build_oof_predictions(
    features: pd.DataFrame,
    oof_path: Path = DEFAULT_OOF_PATH,
    manifest_path: Path = DEFAULT_OOF_MANIFEST_PATH,
    force_refresh: bool = False,
) -> pd.DataFrame:
    shape_key = {
        "n_rows": len(features),
        "feature_columns": FEATURE_COLUMNS,
        "n_folds": features["fold"].nunique(),
        "tree_params": TREE_PARAMS,
        "monotone_up": sorted(MONOTONE_UP),
    }
    if not force_refresh and oof_path.exists() and _oof_manifest_matches(manifest_path, shape_key):
        return pd.read_parquet(oof_path)

    df = features.sort_values("wikidata_qid").reset_index(drop=True)

    binary_scores, binary_models = train_oof(df, "binary")
    rank_scores, rank_models = train_oof(df, "rank")

    labels = df["label"].to_numpy()
    _, binary_calibrated = calibrate(binary_scores, labels)
    _, rank_calibrated = calibrate(rank_scores, labels)

    result = df[["wikidata_qid", "inat_taxon_id", "label", "fold", "no_answer_reason"]].copy()
    result["binary_raw_score"] = binary_scores
    result["binary_calibrated_prob"] = binary_calibrated
    result["rank_raw_score"] = rank_scores
    result["rank_calibrated_prob"] = rank_calibrated

    oof_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(oof_path, index=False)
    shape_key["binary_avg_best_iteration"] = _avg_best_iteration(binary_models)
    shape_key["rank_avg_best_iteration"] = _avg_best_iteration(rank_models)
    manifest_path.write_text(json.dumps(shape_key, indent=2))
    return result


def build_final_models(
    features: pd.DataFrame,
    model_dir: Path = DEFAULT_MODEL_DIR,
    manifest_path: Path = DEFAULT_OOF_MANIFEST_PATH,
    force_refresh: bool = False,
) -> dict:
    """Refit both variants on the full dataset and save them (+ their OOF-fit calibrators) for
    milestone 7's gold-set scoring. Requires build_oof_predictions() to have run first (reads
    the avg_best_iteration values its manifest recorded, and needs the OOF scores to fit the
    calibrators against)."""
    if not manifest_path.exists():
        raise RuntimeError("run build_oof_predictions() first")
    manifest = json.loads(manifest_path.read_text())

    oof = pd.read_parquet(DEFAULT_OOF_PATH)
    df = features.sort_values("wikidata_qid").reset_index(drop=True)
    labels = oof["label"].to_numpy()

    model_dir.mkdir(parents=True, exist_ok=True)
    models = {}
    for objective, score_col, n_est_key in [
        ("binary", "binary_raw_score", "binary_avg_best_iteration"),
        ("rank", "rank_raw_score", "rank_avg_best_iteration"),
    ]:
        model_path = model_dir / f"{objective}_model.json"
        calibrator_path = model_dir / f"{objective}_calibrator.pkl"
        if not force_refresh and model_path.exists() and calibrator_path.exists():
            model = (xgboost.XGBClassifier() if objective == "binary" else xgboost.XGBRanker())
            model.load_model(model_path)
            calibrator = pickle.loads(calibrator_path.read_bytes())
        else:
            model = train_final_model(df, objective, n_estimators=manifest[n_est_key])
            model.save_model(model_path)
            calibrator, _ = calibrate(oof[score_col].to_numpy(), labels)
            calibrator_path.write_bytes(pickle.dumps(calibrator))
        models[objective] = (model, calibrator)
    return models


if __name__ == "__main__":
    from .features import DEFAULT_FEATURES_PATH

    features = pd.read_parquet(DEFAULT_FEATURES_PATH)
    oof = build_oof_predictions(features)
    labels = oof["label"].to_numpy()

    for variant, prob_col in [("binary:logistic", "binary_calibrated_prob"), ("rank:map", "rank_calibrated_prob")]:
        probs = oof[prob_col].to_numpy()
        table = precision_at_threshold_table(probs, labels)
        auto_accept = find_auto_accept_threshold(table)
        reject = find_reject_threshold(probs, labels)
        top1, mrr = top1_accuracy_and_mrr(
            oof[["wikidata_qid", "label", prob_col]], prob_col
        )
        brier = brier_score(probs, labels)

        print(f"\n=== {variant} ===")
        print(f"top-1 accuracy: {top1:.4f}   MRR: {mrr:.4f}   Brier: {brier:.4f}")
        if auto_accept is not None:
            print(
                f"auto-accept @ threshold {auto_accept['threshold']:.2f}: "
                f"precision={auto_accept['precision']:.4f}, coverage={auto_accept['coverage']:.4f}"
            )
        else:
            print(f"no threshold reaches {AUTO_ACCEPT_PRECISION:.1%} precision")
        print(f"reject threshold: {reject}")
