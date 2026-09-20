"""MLflow tracking and the model registry. Spec §7 milestone 15.

All MLflow contact lives here, so `train.py` and `evaluate.py` keep the shape they had and the
instrumentation is strictly additive — not one existing print moves.

**Off unless MLFLOW_TRACKING_URI is set.** With it unset, `mlflow` is never imported, no run is
created, training and scoring behave exactly as before, and `resolve_model()` reads the committed
`data/models/` files. That is what keeps the five-minute path, the container and CI working with
no server and no network. With it set but `mlflow` not installed, this raises rather than
no-opping: silently ignoring an operator who explicitly asked for tracking is the worst available
outcome.

The registry replaces the prose freeze. `CLAUDE.md` currently keeps the milestone 6/7 models
fixed with a paragraph and an existence check; a registered version with an alias is a mechanism.
`data/models/` stays committed as the champion's export and the offline fallback.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import MODEL_DIR, REPO_ROOT

EXPERIMENT = "inat-wikidata-match"

# Short name -> the objective string spec §5 names. The short form keys metrics and model names;
# the long form is a run tag and stays in the existing prints. Every call site derives its variant
# list from this rather than repeating the pair, which is what reconciles the five places the
# binary/rank x raw/calibrated cross product appears with three different conventions.
OBJECTIVES: dict[str, str] = {"binary": "binary:logistic", "rank": "rank:map"}

REGISTERED_MODEL = {objective: f"inat-match-{objective}" for objective in OBJECTIVES}
CHAMPION_ALIAS = "champion"

MODEL_ARTIFACT = "model"

_MODELS_FROM_CODE = Path(__file__).resolve().parent / "mlflow_model.py"

# The artifact store is proxied through the tracking server (--serve-artifacts), so clients need
# no S3 credentials and no boto3. That holds for uploads, but *not* for downloads unless these are
# set: with an S3-backed destination the server advertises multipart downloads via /server-info,
# and mlflow_artifacts_repo._download_file() then requests a presigned URL and fetches the
# storage endpoint directly. That endpoint (http://minio:9000) resolves on the docker network and
# not from the host, so the download hangs rather than failing. See docs/platform.md.
_PROXY_ONLY_ENV = {
    "MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD": "false",
    "MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD": "false",
}


def enabled() -> bool:
    """Truthiness, not membership: compose passes ${MLFLOW_TRACKING_URI:-}, which sets the name
    to an empty string, and `"..." in os.environ` would call that tracking-on. Read at call time
    so tests can set and unset it without reloading the module."""
    return bool(os.environ.get("MLFLOW_TRACKING_URI"))


def _mlflow():
    """Import mlflow, or fail loudly. Never called unless enabled()."""
    for key, value in _PROXY_ONLY_ENV.items():
        os.environ.setdefault(key, value)
    try:
        import mlflow
    except ImportError as exc:  # pragma: no cover - exercised via the sentinel test
        raise SystemExit(
            "MLFLOW_TRACKING_URI is set but mlflow is not installed. Install the tracking extra:\n"
            "    uv sync --extra tracking\n"
            "or unset MLFLOW_TRACKING_URI to run without tracking."
        ) from exc
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    return mlflow


# -- provenance --------------------------------------------------------------------------------


def git_sha() -> tuple[str | None, bool]:
    """(sha, dirty). No manifest in this project records the code version — platform-design §1
    names that as the gap MLflow closes. MATCHER_GIT_SHA first because .dockerignore excludes
    .git/, so `git rev-parse` cannot work inside the image and the SHA has to be injected."""
    injected = os.environ.get("MATCHER_GIT_SHA")
    if injected:
        return injected, False
    try:
        sha = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
        )
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return None, False


def params_blob(extra: Mapping[str, Any] | None = None) -> dict:
    """Every constant that decides what the model is, gathered from the four modules that own
    them, plus the git SHA.

    The imports are function-local because train.py imports this module: at module level the
    cycle would close. Each stage keeping its own constant is right — this is a view over them,
    which is exactly what MLflow wants.
    """
    from .candidates import MAX_EDIT_DISTANCE, K
    from .features import N_SPLITS
    from .labels import RANDOM_SEED, SYNTHETIC_DROPOUT_FRACTION
    from .train import FEATURE_COLUMNS, MONOTONE_DOWN, MONOTONE_UP, RANDOM_STATE, TREE_PARAMS

    sha, dirty = git_sha()
    blob: dict[str, Any] = {
        "git_sha": sha,
        "git_dirty": dirty,
        # Ordered, never sorted: monotone_constraints_tuple() maps MONOTONE_UP onto this list by
        # index, so the order is part of the model's definition (platform-design §4.3.3).
        "feature_columns": list(FEATURE_COLUMNS),
        "n_features": len(FEATURE_COLUMNS),
        "monotone_up": sorted(MONOTONE_UP),
        "monotone_down": sorted(MONOTONE_DOWN),
        "n_splits": N_SPLITS,
        "random_state": RANDOM_STATE,
        "label_random_seed": RANDOM_SEED,
        "synthetic_dropout_fraction": SYNTHETIC_DROPOUT_FRACTION,
        "candidate_k": K,
        "max_edit_distance": MAX_EDIT_DISTANCE,
        **{f"tree_{k}": v for k, v in TREE_PARAMS.items()},
    }
    if extra:
        blob.update(extra)
    return blob


# -- runs and metrics --------------------------------------------------------------------------


def metric_key(population: str, objective: str, metric: str, score: str | None = None) -> str:
    """`{population}.{objective}[.{score}].{metric}` — one vocabulary for the binary/rank x
    raw/calibrated cross product that today appears with three different conventions across
    train.__main__, evaluate.oof_reference, evaluate.score_gold_set,
    evaluate.review_queue_reduction and build_figures._bands."""
    parts = [population, objective] + ([score] if score else []) + [metric]
    return ".".join(parts)


@contextmanager
def run(name: str, tags: Mapping[str, str] | None = None) -> Iterator[Any]:
    """Start a run, or yield None when tracking is off."""
    if not enabled():
        yield None
        return
    mlflow = _mlflow()
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=name, tags=dict(tags or {})) as active:
        yield active


@contextmanager
def resume(run_id: str | None) -> Iterator[Any]:
    """Reopen an existing run, so gold metrics land on the run that produced the model even
    though `evaluate --gold` is a separate process. The run id comes back off the registry
    version (`resolve_model().run_id`), which is what makes an eighth cache manifest unnecessary.
    """
    if not enabled() or run_id is None:
        yield None
        return
    mlflow = _mlflow()
    with mlflow.start_run(run_id=run_id) as active:
        yield active


def log_params(params: Mapping[str, Any]) -> None:
    if not enabled():
        return
    _mlflow().log_params(dict(params))


def log_metrics(metrics: Mapping[str, float], step: int | None = None) -> None:
    """Skips values that are not real numbers.

    `score_gold_set` reports mrr/brier as None for the baseline, and the gold threshold checks
    report NaN precision when no row clears the threshold — which is the actual result here, not
    an edge case. Logging NaN would record a metric that reads as a measurement; skipping it
    records that there was nothing to measure.

    Anything else that is not a real number is skipped rather than raised on. Tracking is
    auxiliary: a stray non-numeric should not take down the run it is describing, and the failure
    mode when it did was worse than useless — half the champion's metrics were rewritten and half
    left stale, with nothing indicating which was which.
    """
    if not enabled():
        return
    mlflow = _mlflow()
    for key, value in metrics.items():
        if value is None or isinstance(value, bool | str):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric != numeric:  # NaN
            continue
        mlflow.log_metric(key, numeric, step=step)


def set_tags(tags: Mapping[str, str]) -> None:
    if not enabled():
        return
    _mlflow().set_tags(dict(tags))


def log_text(text: str, artifact_file: str) -> None:
    if not enabled():
        return
    _mlflow().log_text(text, artifact_file)


def client():
    """The MlflowClient, for reading runs and versions back. Only valid when enabled()."""
    return _mlflow().MlflowClient()


# -- models ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """Where a model came from, alongside the model. `source` and `run_id` are what let callers
    report provenance instead of assuming it."""

    booster: Any
    calibrator: Any
    source: str  # "registry" | "committed"
    uri: str
    run_id: str | None = None
    version: str | None = None

    def score(self, features):
        """(raw, calibrated). The same two numbers the pyfunc artifact returns."""
        from .train import _prepare_X

        X = _prepare_X(features)
        raw = (
            self.booster.predict_proba(X)[:, 1]
            if hasattr(self.booster, "predict_proba")
            else self.booster.predict(X)
        )
        return raw, self.calibrator.predict(raw)


def _committed_paths(objective: str, model_dir: Path) -> tuple[Path, Path]:
    return model_dir / f"{objective}_model.json", model_dir / f"{objective}_calibrator.pkl"


def _load_committed(objective: str, model_dir: Path) -> ResolvedModel:
    import pickle

    import xgboost

    model_path, calibrator_path = _committed_paths(objective, model_dir)
    if not model_path.exists() or not calibrator_path.exists():
        raise SystemExit(
            f"no model for '{objective}': {model_path} or {calibrator_path} is missing.\n"
            "Either set MLFLOW_TRACKING_URI to resolve from the registry, or run "
            "`make final-models` to build them."
        )
    booster = xgboost.XGBClassifier() if objective == "binary" else xgboost.XGBRanker()
    booster.load_model(model_path)
    calibrator = pickle.loads(calibrator_path.read_bytes())
    return ResolvedModel(booster, calibrator, "committed", str(model_path))


def log_and_register(
    objective: str,
    model_path: Path,
    calibrator_path: Path,
    input_example=None,
    tags: Mapping[str, str] | None = None,
) -> str | None:
    """Log the booster and its calibrator as ONE registered pyfunc version, plus the raw booster
    alongside for SHAP (which needs a real Booster, not a pyfunc) and for export back to
    data/models/. Both live in the same run, so they cannot be mismatched.

    Returns the new version number, or None when tracking is off.
    """
    if not enabled():
        return None
    from .train import FEATURE_COLUMNS

    mlflow = _mlflow()
    mlflow.pyfunc.log_model(
        name=MODEL_ARTIFACT,
        python_model=str(_MODELS_FROM_CODE),
        model_config={"objective": objective, "feature_columns": list(FEATURE_COLUMNS)},
        # The booster and its calibrator, in one version. This is the pairing guarantee.
        artifacts={"model": str(model_path), "calibrator": str(calibrator_path)},
        registered_model_name=REGISTERED_MODEL[objective],
        input_example=input_example,
        tags=dict(tags or {}),
    )
    client = mlflow.MlflowClient()
    versions = client.search_model_versions(f"name='{REGISTERED_MODEL[objective]}'")
    return max(versions, key=lambda v: int(v.version)).version


def load_from_dir(objective: str, model_dir: Path) -> ResolvedModel:
    """A booster/calibrator pair from any directory — a challenger's run directory, not only the
    committed export. Independent of whether tracking is on."""
    return _load_committed(objective, model_dir)


def set_alias(objective: str, alias: str, version: str) -> None:
    if not enabled():
        return
    _mlflow().MlflowClient().set_registered_model_alias(REGISTERED_MODEL[objective], alias, version)


def set_champion(objective: str, version: str) -> None:
    set_alias(objective, CHAMPION_ALIAS, version)


def resolve_model(objective: str, model_dir: Path = MODEL_DIR) -> ResolvedModel:
    """The champion from the registry when tracking is on, the committed files otherwise.

    Replaces three hardcoded constructions of the same two paths (train.build_final_models,
    evaluate.score_gold_with_model, build_figures.shap_figure). The booster comes from the
    run's xgboost artifact rather than by reaching inside the pyfunc, and the calibrator from the
    same run — one run id, so the pair is the pair that was registered together.
    """
    if not enabled():
        return _load_committed(objective, model_dir)

    mlflow = _mlflow()
    name = REGISTERED_MODEL[objective]
    uri = f"models:/{name}@{CHAMPION_ALIAS}"
    client = mlflow.MlflowClient()
    try:
        version = client.get_model_version_by_alias(name, CHAMPION_ALIAS)
    except Exception:
        # No champion yet (before the milestone 15 backfill) is a normal state, not an error:
        # fall back to the committed export rather than failing the pipeline.
        return _load_committed(objective, model_dir)

    booster, calibrator = _pair_from_version(mlflow, uri, objective)
    return ResolvedModel(
        booster=booster,
        calibrator=calibrator,
        source="registry",
        uri=uri,
        run_id=version.run_id,
        version=version.version,
    )


def _pair_from_version(mlflow, uri: str, objective: str):
    """Both objects out of the *same* registered version's artifacts.

    Downloading the version and reading the two files is deliberate. Loading the pyfunc and
    reaching through `_model_impl.python_model` for the booster would work but depends on MLflow
    internals, and fetching the booster from one place and the calibrator from another is exactly
    the mismatch this design exists to prevent. Filenames are globbed rather than assumed: MLflow
    stores each artifact under its source basename.
    """
    import pickle

    import xgboost

    local = Path(mlflow.artifacts.download_artifacts(artifact_uri=uri)) / "artifacts"
    model_file = next(local.glob("*_model.json"))
    calibrator_file = next(local.glob("*_calibrator.pkl"))

    booster = xgboost.XGBClassifier() if objective == "binary" else xgboost.XGBRanker()
    booster.load_model(model_file)
    return booster, pickle.loads(calibrator_file.read_bytes())


def export_champion(objective: str, model_dir: Path = MODEL_DIR) -> ResolvedModel:
    """Write the champion back down to data/models/, so the committed export and the alias cannot
    drift. The committed files are what the five-minute path and CI's offline `make gold` use."""
    import pickle

    resolved = resolve_model(objective, model_dir)
    if resolved.source != "registry":
        return resolved
    model_path, calibrator_path = _committed_paths(objective, model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    resolved.booster.save_model(model_path)
    calibrator_path.write_bytes(pickle.dumps(resolved.calibrator))
    return resolved
