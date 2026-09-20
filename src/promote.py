"""Train a challenger, score it against the champion, and promote or hold. Spec §7 milestone 16.

The rule is `docs/findings.md` §10's pre-registered one, applied by code instead of by hand, with
the two things §10 left implicit made precise (`docs/platform-design.md` §5.4, amendment 5):

1. **Eligibility.** For *each* objective, the challenger's OOF top-1 (raw score) may be at most
   `OOF_TOLERANCE` below the champion's. Failing this is a **regression**.
2. **Gold top-1** on `RANKING_OBJECTIVE`, counted in missed items, with a ±`GOLD_ITEM_BAND`
   equivalence band. More than two items worse is a **regression**; more than two better wins.
3. **Gold score band**: wrong rows among gold rows with a raw `binary:logistic` score in
   `[BAND_LO, BAND_HI)` — the band is defined on the binary model's scores whatever the ranking
   objective, which is how milestone 6 defined it. ±`BAND_ROW_BAND` rows is a tie. §10 called 4,
   4 and 3 wrong rows "tied", and counting rows rather than comparing precision keeps that
   verdict while the band's own size moves (164–183 rows across the ladder).
4. **Gold Brier** on `RANKING_OBJECTIVE`: strictly lower wins.
5. Otherwise the champion keeps its place.

Three outcomes, deliberately distinct: *promote*; *hold* (not better — the challenger is registered
under the `challenger` alias, nothing else changes, and it is not an error); *regress* (the
orchestrator fails the task, so a red run means something went wrong rather than "no
improvement this time").

The champion is re-scored on the gold set *as it is now* every time, never compared through its
logged gold metrics: labels are added and corrected (§10's `Q14908802`), and a comparison across two
versions of the measuring instrument measures the instrument. Its OOF top-1 does come from its
registered run, because recomputing it means retraining it.

    export MLFLOW_TRACKING_URI=http://localhost:5000
    .venv/bin/python -m src.promote                  # train, score, register, decide
    .venv/bin/python -m src.promote --override-version 7 --reason "..."   # a human decision
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from . import tracking
from .evaluate import (
    BAND_HI,
    BAND_LO,
    gold_metrics,
    gold_shared_metrics,
    gold_top1_misses,
    load_gold_features,
    oof_reference,
    score_gold_set,
)
from .features import DEFAULT_FEATURES_PATH
from .paths import DATA_DIR
from .train import (
    DEFAULT_OOF_MANIFEST_PATH,
    DEFAULT_OOF_PATH,
    FEATURE_COLUMNS,
    build_final_models,
    build_oof_predictions,
    oof_metrics,
)

OOF_TOLERANCE = 0.001  # 0.1pp, as pre-registered
GOLD_ITEM_BAND = 2  # "anything on this gold set smaller than about two items should be read as noise"
BAND_ROW_BAND = 2
RANKING_OBJECTIVE = "rank"  # the reported default since milestone 15

CHALLENGER_ALIAS = "challenger"
RUNS_DIR = DATA_DIR / "runs"

PROMOTE, HOLD, REGRESS = "promote", "hold", "regress"

_OOF_NAME = "oof_predictions.parquet"
_MANIFEST_NAME = "oof_predictions.manifest.json"


# -- the rule ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scorecard:
    """Everything the rule reads, for one model pair. Counts, not rates, where the rule compares
    in items."""

    oof_top1: dict[str, float]  # objective -> raw-score OOF top-1
    gold_misses: int  # RANKING_OBJECTIVE, answerable items whose top-1 is wrong
    gold_answerable: int
    band_wrong: int
    band_n: int
    gold_brier: float  # RANKING_OBJECTIVE, calibrated


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reasons: tuple[str, ...]


def decide(challenger: Scorecard, champion: Scorecard) -> Verdict:
    """The pre-registered rule. Pure, so it can be tested against §10's recorded decisions."""
    reasons: list[str] = []

    for objective in sorted(champion.oof_top1):
        drop = champion.oof_top1[objective] - challenger.oof_top1[objective]
        # A tiny epsilon so a drop of exactly the tolerance, after float arithmetic, is not
        # misread as a regression.
        if drop > OOF_TOLERANCE + 1e-12:
            return Verdict(REGRESS, (
                f"ineligible: OOF top-1 {objective} fell {drop * 100:.3f}pp "
                f"({champion.oof_top1[objective]:.5f} -> {challenger.oof_top1[objective]:.5f}), "
                f"tolerance {OOF_TOLERANCE * 100:.1f}pp",
            ))
        reasons.append(f"eligible on {objective}: OOF top-1 change {-drop * 100:+.3f}pp")

    misses = challenger.gold_misses - champion.gold_misses
    detail = f"gold top-1 {RANKING_OBJECTIVE}: {challenger.gold_misses} vs {champion.gold_misses} misses"
    if misses > GOLD_ITEM_BAND:
        return Verdict(REGRESS, (*reasons, f"{detail} — more than {GOLD_ITEM_BAND} items worse"))
    if misses < -GOLD_ITEM_BAND:
        return Verdict(PROMOTE, (*reasons, f"{detail} — more than {GOLD_ITEM_BAND} items better"))
    reasons.append(f"{detail} — within ±{GOLD_ITEM_BAND}, tie")

    wrong = challenger.band_wrong - champion.band_wrong
    detail = (f"gold band wrong rows: {challenger.band_wrong}/{challenger.band_n} vs "
              f"{champion.band_wrong}/{champion.band_n}")
    if wrong < -BAND_ROW_BAND:
        return Verdict(PROMOTE, (*reasons, f"{detail} — more than {BAND_ROW_BAND} fewer"))
    if wrong > BAND_ROW_BAND:
        return Verdict(HOLD, (*reasons, f"{detail} — more than {BAND_ROW_BAND} more"))
    reasons.append(f"{detail} — within ±{BAND_ROW_BAND}, tie")

    detail = f"gold Brier {RANKING_OBJECTIVE}: {challenger.gold_brier:.4f} vs {champion.gold_brier:.4f}"
    if challenger.gold_brier < champion.gold_brier:
        return Verdict(PROMOTE, (*reasons, f"{detail} — lower"))
    return Verdict(HOLD, (*reasons, f"{detail} — not lower; the champion keeps its place"))


def gold_scorecard_part(features: pd.DataFrame, brier: float) -> dict:
    """The gold half of a Scorecard, from a frame score_gold_set() has attached scores to."""
    has_positive = features.groupby("wikidata_qid")["label"].max()
    band = (features["binary_raw_score"] >= BAND_LO) & (features["binary_raw_score"] < BAND_HI)
    return {
        "gold_misses": int(gold_top1_misses(features, RANKING_OBJECTIVE)["wikidata_qid"].nunique()),
        "gold_answerable": int((has_positive == 1).sum()),
        "band_wrong": int((features.loc[band, "label"] == 0).sum()),
        "band_n": int(band.sum()),
        "gold_brier": float(brier),
    }


def _flatten(prefix: str, values: dict) -> dict[str, float]:
    """{'oof_top1': {'binary': x}, 'band_n': n} -> {'<prefix>.oof_top1.binary': x, '<prefix>.band_n': n}"""
    flat = {}
    for key, value in values.items():
        if isinstance(value, dict):
            flat.update(_flatten(f"{prefix}.{key}", value))
        else:
            flat[f"{prefix}.{key}"] = value
    return flat


def _oof_top1(oof: pd.DataFrame) -> dict[str, float]:
    return {
        objective: oof_metrics(oof, objective)[tracking.metric_key("oof", objective, "top1", "raw")]
        for objective in tracking.OBJECTIVES
    }


def _champion_oof_top1(run_id: str) -> dict[str, float]:
    metrics = tracking.client().get_run(run_id).data.metrics
    try:
        return {
            objective: metrics[tracking.metric_key("oof", objective, "top1", "raw")]
            for objective in tracking.OBJECTIVES
        }
    except KeyError as exc:
        raise SystemExit(
            f"the champion's run {run_id} has no {exc.args[0]} metric — cannot apply the OOF "
            "eligibility check without it"
        ) from exc


# -- the stages the DAG runs -------------------------------------------------------------------


def new_run_dir(label: str | None = None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / (label or stamp)
    path.mkdir(parents=True, exist_ok=False)
    return path


def train(run_dir: Path, features_path: Path = DEFAULT_FEATURES_PATH) -> None:
    """OOF predictions and the two refitted models, into `run_dir` and nowhere else.

    A fresh directory per challenger, so the OOF cache can never hand back a previous run's
    predictions: that cache is keyed on the feature table's content and the hyperparameters, not on
    the code, and a code-only change is exactly what this pipeline exists to measure.
    """
    features = pd.read_parquet(features_path)
    print(f"training a challenger on {len(features):,} rows into {run_dir}")
    build_oof_predictions(
        features, oof_path=run_dir / _OOF_NAME, manifest_path=run_dir / _MANIFEST_NAME,
        features_path=features_path,
    )
    build_final_models(
        features, model_dir=run_dir, manifest_path=run_dir / _MANIFEST_NAME, oof_path=run_dir / _OOF_NAME,
    )


def evaluate_and_register(
    run_dir: Path, features_path: Path = DEFAULT_FEATURES_PATH, tags: dict | None = None,
) -> dict:
    """Score both pairs on the current gold set, log everything, register the challenger, decide.

    Returns a JSON-serialisable summary (it travels between Airflow tasks as an XCom).
    """
    if not tracking.enabled():
        raise SystemExit(
            "MLFLOW_TRACKING_URI is not set — the gate compares against the registered champion.\n"
            "`make platform-up`, then export what `make platform-url` prints."
        )

    champions = {o: tracking.resolve_model(o) for o in tracking.OBJECTIVES}
    if any(m.source != "registry" for m in champions.values()):
        raise SystemExit("no registered champion to compare against — run backfill_v1.py first")
    champion_versions = {o: m.version for o, m in champions.items()}
    champion_run = champions[RANKING_OBJECTIVE].run_id
    challengers = {o: tracking.load_from_dir(o, run_dir) for o in tracking.OBJECTIVES}

    gold = load_gold_features()
    challenger_gold = score_gold_set(gold.copy(), models=challengers)
    champion_gold = score_gold_set(gold.copy(), models=champions)

    oof = pd.read_parquet(run_dir / _OOF_NAME)
    challenger_card = Scorecard(
        oof_top1=_oof_top1(oof),
        **gold_scorecard_part(challenger_gold["features"], challenger_gold["metrics"][RANKING_OBJECTIVE]["brier"]),
    )
    champion_card = Scorecard(
        oof_top1=_champion_oof_top1(champion_run),
        **gold_scorecard_part(champion_gold["features"], champion_gold["metrics"][RANKING_OBJECTIVE]["brier"]),
    )
    verdict = decide(challenger_card, champion_card)

    # The challenger's thresholds come from its own OOF; data/oof_predictions.parquet is the
    # champion's and would score the challenger against somebody else's thresholds.
    reference = oof_reference(oof)
    sha, dirty = tracking.git_sha()
    features = pd.read_parquet(features_path, columns=FEATURE_COLUMNS).head(3)
    run_tags = {
        "stage": "challenger",
        "tree_dirty": str(dirty),
        "run_dir": str(run_dir.relative_to(DATA_DIR)),
        "gate_verdict": verdict.outcome,
        "gate_reasons": "\n".join(verdict.reasons),
        "gate_champion_versions": ",".join(f"{o}=v{v}" for o, v in champion_versions.items()),
        **(tags or {}),
    }
    versions = {}
    with tracking.run(f"challenger-{(sha or 'nogit')[:8]}", tags=run_tags) as active:
        tracking.log_params(tracking.params_blob())
        manifest = (run_dir / _MANIFEST_NAME).read_text()
        tracking.log_metrics(gold_shared_metrics(challenger_gold, reference))
        for objective, long_name in tracking.OBJECTIVES.items():
            tracking.log_metrics(oof_metrics(oof, objective))
            tracking.log_metrics(gold_metrics(challenger_gold, reference, objective))
            versions[objective] = tracking.log_and_register(
                objective,
                run_dir / f"{objective}_model.json",
                run_dir / f"{objective}_calibrator.pkl",
                input_example=features,
                tags={"objective": long_name, "gate_verdict": verdict.outcome},
            )
        # The champion's side of the comparison, as it was measured *now*. Logged on the
        # challenger's run so one run page shows both sides of the decision.
        for side, card in (("challenger", challenger_card), ("champion", champion_card)):
            tracking.log_metrics(_flatten(f"gate.{side}", asdict(card)))
        tracking.log_text(manifest, "oof_predictions.manifest.json")
        run_id = active.info.run_id

    for objective, version in versions.items():
        if verdict.outcome != PROMOTE:
            tracking.set_alias(objective, CHALLENGER_ALIAS, version)

    summary = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "outcome": verdict.outcome,
        "reasons": list(verdict.reasons),
        "versions": versions,
        "champion_versions": champion_versions,
        "challenger": asdict(challenger_card),
        "champion": asdict(champion_card),
    }
    print_summary(summary)
    return summary


def promote(run_dir: Path, versions: dict[str, str]) -> None:
    """Move the champion alias, and bring the committed copies along with it.

    `data/models/` is what the five-minute path and CI score with, and `data/oof_predictions.*`
    holds the thresholds gold scoring re-applies — both must belong to the same model as the
    alias, as milestone 15's promotion established. The result is a git diff for a human to
    review and commit; nothing here commits.
    """
    for objective, version in versions.items():
        tracking.set_champion(objective, version)
        tracking.export_champion(objective)
        print(f"  {tracking.REGISTERED_MODEL[objective]}@{tracking.CHAMPION_ALIAS} -> v{version}")
    shutil.copyfile(run_dir / _OOF_NAME, DEFAULT_OOF_PATH)
    shutil.copyfile(run_dir / _MANIFEST_NAME, DEFAULT_OOF_MANIFEST_PATH)
    print("  data/models/ and data/oof_predictions.* now hold the new champion — review and commit")


def override(version: str, reason: str) -> None:
    """Promote a registered version by hand, recording why.

    §10's own precedent: a correctness fix is not subject to a metrics vote. The reason is tagged
    on the run so the deviation stays visible next to the numbers that did not justify it.
    """
    client = tracking.client()
    run_ids = {client.get_model_version(tracking.REGISTERED_MODEL[o], version).run_id
               for o in tracking.OBJECTIVES}
    if len(run_ids) != 1:
        raise SystemExit(f"v{version} of the two registered models come from different runs: {run_ids}")
    run_id = run_ids.pop()
    run = client.get_run(run_id)
    run_dir = run.data.tags.get("run_dir")
    if not run_dir:
        raise SystemExit(f"run {run_id} has no run_dir tag — only gate-produced versions can be overridden")
    client.set_tag(run_id, "gate_override", reason)
    promote(DATA_DIR / run_dir, {o: version for o in tracking.OBJECTIVES})


def print_summary(summary: dict) -> None:
    print(f"\ngate verdict: {summary['outcome'].upper()}")
    for reason in summary["reasons"]:
        print(f"  - {reason}")
    print(f"challenger registered as {summary['versions']} (run {summary['run_id']}); "
          f"champion was {summary['champion_versions']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path,
                        help="an existing run directory to evaluate, skipping training")
    parser.add_argument("--override-version", help="promote this registered version by hand")
    parser.add_argument("--reason", help="required with --override-version")
    parser.add_argument("--dry-run", action="store_true",
                        help="train, score, register and decide, but never move the champion alias "
                             "or overwrite data/models/ — what a verification run wants")
    args = parser.parse_args()

    if args.override_version:
        if not args.reason:
            parser.error("--override-version needs --reason")
        override(args.override_version, args.reason)
        raise SystemExit(0)

    run_dir = args.run_dir or new_run_dir()
    if not args.run_dir:
        train(run_dir)
    summary = evaluate_and_register(run_dir)
    if summary["outcome"] == PROMOTE and not args.dry_run:
        promote(run_dir, summary["versions"])
    elif summary["outcome"] == PROMOTE:
        print("(--dry-run: the champion alias and data/models/ are untouched)")
    raise SystemExit(1 if summary["outcome"] == REGRESS else 0)
