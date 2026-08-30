"""Train, score and register one rung of the milestone 15 ladder.

Spec §7 milestone 15 releases the model freeze and retrains. It does so as a *ladder* — one
registered version per change — rather than one combined retrain, so that every delta is
attributable to the thing that caused it. `docs/future-work.md` asks for exactly this for the
monotone-constraint change: *"a deliberate, fully-rescored comparison ... rather than a patch"*.

Each rung is one commit plus one invocation of this script at that commit:

    export MLFLOW_TRACKING_URI=http://localhost:5000
    .venv/bin/python run_ladder.py --rung v2

    | rung | what changed                                    |
    |------|-------------------------------------------------|
    | v1   | the frozen binaries (backfill_v1.py, not here)  |
    | v2   | deterministic feature row order, nothing else   |
    | v3   | the dbt feature table becomes canonical         |
    | v4   | the strategy_* one-hot substring fix            |
    | v5   | MONOTONE_UP extended (+ MONOTONE_DOWN)          |

**v2 exists to measure the noise floor.** It changes no feature definition at all — only the
order rows are written in. Its delta is therefore what a pure `subsample` reshuffle is worth, and
nothing later can be called a real improvement unless it clears that. At n=263 gold items, two
items is 0.87pp, so the floor matters.

Models go to `data/ladder/<rung>/`, never over `data/models/`. The committed export stays the
champion's until something beats it, which keeps the five-minute path and CI reproducing numbers
the README actually states. Gold scoring for a rung points MATCHER_MODEL_DIR at that directory,
which is why the rung's own models are used rather than the registry's champion.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import pandas as pd

from src import tracking
from src.features import DEFAULT_FEATURES_PATH
from src.paths import DATA_DIR
from src.train import (
    DEFAULT_OOF_MANIFEST_PATH,
    FEATURE_COLUMNS,
    build_final_models,
    build_oof_predictions,
    oof_metrics,
)

RUNGS = {
    "v2": "deterministic feature row order",
    "v3": "dbt feature table becomes canonical",
    "v4": "strategy_* one-hot substring fix",
    "v5": "MONOTONE_UP extended",
}


def ladder_dir(rung: str):
    return DATA_DIR / "ladder" / rung


def gold_metrics_for_rung(rung: str) -> dict:
    """Score the gold set with *this rung's* models, in a subprocess.

    A subprocess because model resolution reads MATCHER_MODEL_DIR at import time via paths.py,
    and because tracking has to be off for the run so resolve_model() takes the rung's directory
    instead of the registry's champion. Cleaner than reloading modules in place.
    """
    env = dict(os.environ)
    env["MATCHER_MODEL_DIR"] = str(ladder_dir(rung))
    env.pop("MLFLOW_TRACKING_URI", None)
    env["MATCHER_LADDER_JSON"] = "1"

    out = subprocess.run(
        [sys.executable, "-c", _GOLD_SNIPPET], env=env, capture_output=True, text=True,
        cwd=str(DATA_DIR.parent),
    )
    if out.returncode != 0:
        raise SystemExit(f"gold scoring failed for {rung}:\n{out.stdout}\n{out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


_GOLD_SNIPPET = """
import json
from src.evaluate import (gold_band_comparison, load_gold_features, load_oof_reference,
                          score_gold_set)
features = load_gold_features()
result = score_gold_set(features)
reference = load_oof_reference()
band = gold_band_comparison(result["features"], reference)
print(json.dumps({
    "metrics": {k: v for k, v in result["metrics"].items()},
    "recall_ceiling": result["recall_ceiling"],
    "band_n": band["gold_n"],
    "band_precision": band["gold_precision"],
    "n_items": int(result["features"]["wikidata_qid"].nunique()),
}))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rung", required=True, choices=sorted(RUNGS))
    parser.add_argument("--force-refresh", action="store_true",
                        help="retrain even when this rung's cached OOF matches")
    args = parser.parse_args()
    rung = args.rung

    if not tracking.enabled():
        raise SystemExit(
            "MLFLOW_TRACKING_URI is not set — a ladder rung that is not recorded is not a rung.\n"
            "`make platform-up`, then export what `make platform-url` prints."
        )
    if not DEFAULT_FEATURES_PATH.exists():
        raise SystemExit(f"{DEFAULT_FEATURES_PATH} not found — run `make features` first.")

    out_dir = ladder_dir(rung)
    out_dir.mkdir(parents=True, exist_ok=True)
    oof_path = out_dir / "oof_predictions.parquet"
    manifest_path = out_dir / "oof_predictions.manifest.json"

    features = pd.read_parquet(DEFAULT_FEATURES_PATH)
    print(f"[{rung}] {RUNGS[rung]}")
    print(f"[{rung}] training OOF over {len(features):,} rows...")
    oof = build_oof_predictions(
        features, oof_path=oof_path, manifest_path=manifest_path,
        features_path=DEFAULT_FEATURES_PATH, force_refresh=args.force_refresh,
    )

    print(f"[{rung}] refitting final models into {out_dir.relative_to(DATA_DIR.parent)}/...")
    build_final_models(features, model_dir=out_dir, manifest_path=manifest_path, oof_path=oof_path)

    print(f"[{rung}] scoring the gold set with this rung's models...")
    gold = gold_metrics_for_rung(rung)

    sha, dirty = tracking.git_sha()
    manifest = json.loads(manifest_path.read_text())
    with tracking.run(
        f"{rung}-{(sha or 'nogit')[:8]}",
        tags={"stage": "ladder", "ladder": rung, "ladder_change": RUNGS[rung],
              "tree_dirty": str(dirty)},
    ):
        tracking.log_params(tracking.params_blob({"ladder": rung}))
        tracking.log_metrics({
            tracking.metric_key("gold", "band", "n"): gold["band_n"],
            tracking.metric_key("gold", "band", "precision"): gold["band_precision"],
            tracking.metric_key("gold", "baseline", "top1"): gold["metrics"]["baseline"]["top1_accuracy"],
            tracking.metric_key("gold", "recall_ceiling", "value"): gold["recall_ceiling"],
            tracking.metric_key("gold", "items", "n"): gold["n_items"],
        })
        versions = {}
        for objective, long_name in tracking.OBJECTIVES.items():
            variant = gold["metrics"][objective]
            tracking.log_metrics({
                tracking.metric_key("gold", objective, "top1", "raw"): variant["top1_accuracy"],
                tracking.metric_key("gold", objective, "mrr", "raw"): variant["mrr"],
                tracking.metric_key("gold", objective, "brier", "calibrated"): variant["brier"],
            })
            tracking.log_metrics(oof_metrics(oof, objective))
            tracking.log_metrics({
                tracking.metric_key("oof", objective, "avg_best_iteration"):
                    manifest.get(f"{objective}_avg_best_iteration"),
            })
            versions[objective] = tracking.log_and_register(
                objective,
                out_dir / f"{objective}_model.json",
                out_dir / f"{objective}_calibrator.pkl",
                input_example=features[FEATURE_COLUMNS].head(3),
                tags={"objective": long_name, "ladder": rung},
            )

    print(f"\n[{rung}] registered:")
    for objective, version in versions.items():
        print(f"  {tracking.REGISTERED_MODEL[objective]} v{version}")
    print(f"\n[{rung}] gold top-1 (raw):  "
          + "  ".join(f"{o}={gold['metrics'][o]['top1_accuracy']:.4f}" for o in tracking.OBJECTIVES))
    print(f"[{rung}] gold band precision: {gold['band_precision']:.4f} (n={gold['band_n']})")
    print("\nThe champion alias is NOT moved here — that is the promotion step, once every rung "
          "has run and the pre-registered rule can be applied to all of them.")

    # DEFAULT_OOF_MANIFEST_PATH is untouched on purpose: data/models/ and the frozen OOF stay the
    # champion's until promotion.
    assert DEFAULT_OOF_MANIFEST_PATH.exists()


if __name__ == "__main__":
    main()
