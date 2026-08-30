"""Register the frozen milestone 6/7 models as version 1 in the MLflow registry.

Spec §7 milestone 15: *"the existing frozen models are backfilled as version 1 with their
published metrics attached, so the history stays traceable"*. This is what replaces the prose
freeze in `CLAUDE.md` with a mechanism — after it runs, `models:/inat-match-binary@champion`
resolves to the exact binaries every published number is quoted against.

Runs once, and only with a tracking server:

    export MLFLOW_TRACKING_URI=http://localhost:5000     # `make platform-url`
    .venv/bin/python backfill_v1.py

**Metrics are recomputed, never transcribed.** Copying numbers out of README.md into a registry
would record what the docs *say* rather than what the models *do*, which is the opposite of the
traceability this milestone is for. The two families are recomputed differently, and the
difference is recorded on the run rather than smoothed over:

  * **Gold metrics** come from scoring these exact binaries. Fully reproducible — CI proves it
    every push by grepping `make gold`'s output for the committed numbers.
  * **OOF metrics** cannot be. The models are dated 2026-08-23; `.venv` was rebuilt on 2026-08-29
    for milestones 13/14, so the environment that produced them no longer exists and a retrain
    does not reproduce their row-level scores (every one of 590,671 differs; `best_iteration`
    moves 882 -> 918). Not thread count — `n_jobs` in {1,4,8,20} give bit-identical fits. So the
    OOF numbers here are a **fresh recompute of the same configuration in the current
    environment**, tagged `oof_metrics_source=recomputed`, which is also the honest baseline for
    the milestone 15 ladder: comparing v2 against a v1 measured in a different environment would
    charge environment drift to the code change.

The recompute writes to its own path and leaves `data/oof_predictions.parquet` alone. Gold
scoring reads that file for its thresholds (`load_oof_reference`), so overwriting it here would
move `gold_band_comparison` and `gold_threshold_check` — perturbing published numbers in the
slice that is only supposed to record them.
"""

from __future__ import annotations

import pandas as pd

from src import tracking
from src.evaluate import (
    gold_band_comparison,
    load_gold_features,
    load_oof_reference,
    score_gold_set,
)
from src.features import DEFAULT_FEATURES_PATH
from src.paths import DATA_DIR, MODEL_DIR
from src.train import FEATURE_COLUMNS, build_oof_predictions, oof_metrics

# The commit whose tree holds these exact model bytes — verified with `git hash-object`, not
# assumed from the file dates.
FROZEN_AT = "3e59a13"
FROZEN_NOTE = "Close the gold-set study at n=263 / commit the frozen models"

# Deliberately not DEFAULT_OOF_PATH. See the module docstring.
RECOMPUTE_OOF_PATH = DATA_DIR / "oof_predictions.v1_recompute.parquet"
RECOMPUTE_MANIFEST_PATH = DATA_DIR / "oof_predictions.v1_recompute.manifest.json"


def recomputed_oof() -> pd.DataFrame:
    if not DEFAULT_FEATURES_PATH.exists():
        raise SystemExit(
            f"{DEFAULT_FEATURES_PATH} not found. The OOF recompute needs the full feature table — "
            "run `make features` (see README, 'The full path')."
        )
    features = pd.read_parquet(DEFAULT_FEATURES_PATH)
    return build_oof_predictions(
        features,
        oof_path=RECOMPUTE_OOF_PATH,
        manifest_path=RECOMPUTE_MANIFEST_PATH,
        features_path=DEFAULT_FEATURES_PATH,
    )


def main() -> None:
    if not tracking.enabled():
        raise SystemExit(
            "MLFLOW_TRACKING_URI is not set — there is nothing to back-fill into.\n"
            "Bring the stack up with `make platform-up`, then export what `make platform-url` "
            "prints."
        )

    for objective in tracking.OBJECTIVES:
        model_path = MODEL_DIR / f"{objective}_model.json"
        if not model_path.exists():
            raise SystemExit(f"{model_path} not found — data/models/ holds the models to register.")

    print("scoring the frozen models on the gold set...")
    features = load_gold_features()
    result = score_gold_set(features)
    reference = load_oof_reference()
    band = gold_band_comparison(result["features"], reference)

    print("recomputing OOF in the current environment (~2 min, does not touch the frozen cache)...")
    oof = recomputed_oof()

    sha, dirty = tracking.git_sha()
    with tracking.run(
        f"v1-backfill-{FROZEN_AT}",
        tags={
            "stage": "backfill",
            "backfilled": "true",
            "ladder": "v1",
            "frozen_at": FROZEN_AT,
            "frozen_note": FROZEN_NOTE,
            # The run's timestamps are "now"; these say what the artefacts actually are, so the
            # run does not read as if it were produced in August.
            "models_dated": "2026-08-23",
            "oof_metrics_source": "recomputed",
            "oof_metrics_note": (
                "Recomputed in the current environment. The frozen binaries predate uv.lock's "
                "dependency set and their row-level OOF scores are not reproducible; gold metrics "
                "below ARE from these exact binaries."
            ),
            "gold_metrics_source": "frozen-binaries",
            "features": "pandas",
            "strategy_onehot": "substring-bug",
            "monotone": "base",
            "backfill_run_at_sha": sha or "unknown",
            "backfill_run_tree_dirty": str(dirty),
        },
    ):
        params = tracking.params_blob()
        # params_blob() records the SHA this script ran at; the model's provenance is the commit
        # that holds its bytes, which is older. Both, clearly named, rather than one ambiguous one.
        params["git_sha"] = FROZEN_AT
        tracking.log_params(params)

        tracking.log_metrics(
            {
                tracking.metric_key("gold", "band", "n"): band["gold_n"],
                tracking.metric_key("gold", "band", "precision"): band["gold_precision"],
                tracking.metric_key("gold", "baseline", "top1"): result["metrics"]["baseline"][
                    "top1_accuracy"
                ],
                tracking.metric_key("gold", "recall_ceiling", "value"): result["recall_ceiling"],
                tracking.metric_key("gold", "items", "n"): result["features"][
                    "wikidata_qid"
                ].nunique(),
            }
        )

        versions = {}
        for objective, long_name in tracking.OBJECTIVES.items():
            variant = result["metrics"][objective]
            tracking.log_metrics(
                {
                    tracking.metric_key("gold", objective, "top1", "raw"): variant["top1_accuracy"],
                    tracking.metric_key("gold", objective, "mrr", "raw"): variant["mrr"],
                    tracking.metric_key("gold", objective, "brier", "calibrated"): variant["brier"],
                }
            )
            tracking.log_metrics(oof_metrics(oof, objective))
            versions[objective] = tracking.log_and_register(
                objective,
                MODEL_DIR / f"{objective}_model.json",
                MODEL_DIR / f"{objective}_calibrator.pkl",
                input_example=features[FEATURE_COLUMNS].head(3),
                tags={"objective": long_name, "ladder": "v1", "frozen_at": FROZEN_AT},
            )

    for objective, version in versions.items():
        # The registry should never sit in a no-champion state: v1 genuinely is the champion
        # until something beats it, and resolve_model() falls back to data/models/ meanwhile.
        tracking.set_champion(objective, version)
        print(f"  {tracking.REGISTERED_MODEL[objective]} v{version} -> @{tracking.CHAMPION_ALIAS}")

    print("\nverifying the alias resolves to the frozen binaries...")
    for objective in tracking.OBJECTIVES:
        resolved = tracking.resolve_model(objective)
        raw, _ = resolved.score(result["features"])
        matches = bool((raw == result["features"][f"{objective}_raw_score"].to_numpy()).all())
        print(f"  {objective}: {resolved.uri} (v{resolved.version}) scores identically: {matches}")
        if not matches:
            raise SystemExit(f"{objective}: the registered model does not reproduce its own scores")


if __name__ == "__main__":
    main()
