"""The scoring artifact MLflow logs: an XGBoost booster and its isotonic calibrator, together.

This is a **models-from-code** script. MLflow executes this file — at log time to validate it,
and again inside `mlflow.pyfunc.load_model()` — rather than CloudPickling a live object, so the
stored artifact does not depend on this package being importable when it is loaded. Nothing here
imports from `src`, on purpose: that independence is the whole point, and an import of
`src.train` would quietly reintroduce the coupling.

The pairing is the safety property. `CLAUDE.md` records a real bug where a frozen model was
silently matched with a calibrator refit against different OOF data, and `build_final_models()`
guards the pair with an existence check and no manifest. One registered version holding both
makes that mismatch unrepresentable rather than merely documented.

`feature_columns` is carried in the model config rather than imported, so the artifact records
the exact ordered feature list it was trained against. That closes the positional-constraint
hazard in platform-design §4.3.3 at the artifact level: `monotone_constraints_tuple()` maps
`MONOTONE_UP` onto `FEATURE_COLUMNS` by index, so a reordering silently re-targets the
constraints, and a model that carries its own order can be checked rather than trusted.

Returns both scores because the project needs both and they are not interchangeable: within-group
ranking (top-1, MRR) must use `raw_score`, since isotonic calibration is a step function whose
plateaus discard ordering inside a candidate group, while anything needing cross-group
comparability (thresholds, Brier) must use `calibrated_prob`. See `evaluate.ranking_score_column`
and `docs/findings.md` §3.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import mlflow
import pandas as pd
import xgboost
from mlflow.models import ModelConfig, set_model

_config = ModelConfig()
OBJECTIVE: str = _config.get("objective")
FEATURE_COLUMNS: list[str] = list(_config.get("feature_columns"))


class CalibratedMatcher(mlflow.pyfunc.PythonModel):
    def load_context(self, context) -> None:
        cls = xgboost.XGBClassifier if OBJECTIVE == "binary" else xgboost.XGBRanker
        self.booster = cls()
        self.booster.load_model(context.artifacts["model"])
        self.calibrator = pickle.loads(Path(context.artifacts["calibrator"]).read_bytes())

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        # Mirrors train._prepare_X. The cast happens here rather than in the caller so the logged
        # signature carries the true boolean dtypes: inferring it from an already-cast frame makes
        # the columns int32, and predicting with the natural frame then fails schema enforcement
        # with "Can not safely convert bool to int32".
        X = df[FEATURE_COLUMNS].copy()
        for col in X.columns:
            if X[col].dtype == bool:
                X[col] = X[col].astype("int8")
        return X

    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        X = self._prepare(model_input)
        raw = (
            self.booster.predict_proba(X)[:, 1]
            if OBJECTIVE == "binary"
            else self.booster.predict(X)
        )
        return pd.DataFrame(
            {"raw_score": raw, "calibrated_prob": self.calibrator.predict(raw)},
            index=model_input.index,
        )


set_model(CalibratedMatcher())
