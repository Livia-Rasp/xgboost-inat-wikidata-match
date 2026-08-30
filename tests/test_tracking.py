"""Tracking must be invisible when it is off, and loud when it is misconfigured.

Spec §7 milestone 15. The property everything else rests on is that an unset MLFLOW_TRACKING_URI
leaves the pipeline exactly as it was — that is what keeps the five-minute path, the container and
CI working with no server and no network — so most of these tests are about the *absence* of
behaviour, which is the kind that rots silently unless it is pinned.

Network-free: nothing here contacts a tracking server.
"""

from __future__ import annotations

import sys

import pandas as pd
import pytest

from src import tracking
from src.train import FEATURE_COLUMNS, MONOTONE_UP


@pytest.fixture
def tracking_off(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)


@pytest.fixture
def tracking_on(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://localhost:1")


class _Exploding:
    """Stands in for mlflow. Any attribute access at all is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"mlflow was touched while tracking is off (accessed .{name})")


# -- off unless asked for ----------------------------------------------------------------------


def test_tracking_is_off_without_the_env_var(tracking_off):
    assert tracking.enabled() is False


def test_an_empty_env_var_is_off_not_on(monkeypatch):
    """compose.yaml passes ${MLFLOW_TRACKING_URI:-}, which sets the name to an empty string.
    `"MLFLOW_TRACKING_URI" in os.environ` would call that tracking-on and then fail to connect."""
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "")
    assert tracking.enabled() is False


def test_enabled_is_read_at_call_time(monkeypatch, tracking_off):
    """No module reload needed to flip it — the test_paths.py importlib dance would be a smell."""
    assert tracking.enabled() is False
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://localhost:1")
    assert tracking.enabled() is True


def test_nothing_touches_mlflow_when_disabled(monkeypatch, tracking_off):
    """The real guardrail. `pip install -e ".[dev]"` — the README's five-minute path — has no
    mlflow at all, so an import on this path would be an ImportError for every user of it."""
    monkeypatch.setitem(sys.modules, "mlflow", _Exploding())

    with tracking.run("anything") as active:
        assert active is None
    with tracking.resume("some-run-id") as active:
        assert active is None
    tracking.log_params({"a": 1})
    tracking.log_metrics({"b": 2.0})
    tracking.set_tags({"c": "d"})
    assert tracking.log_and_register("binary", "m.json", "c.pkl") is None
    tracking.set_champion("binary", "1")


def test_a_set_uri_without_mlflow_installed_fails_loudly(monkeypatch, tracking_on):
    """Silently no-opping when the operator explicitly asked for tracking is the worst available
    outcome: the run looks fine and nothing is recorded."""
    monkeypatch.setitem(sys.modules, "mlflow", None)  # import mlflow -> ImportError
    with pytest.raises(SystemExit, match="tracking"):
        tracking._mlflow()


# -- model resolution --------------------------------------------------------------------------


def test_resolve_model_falls_back_to_the_committed_files(tracking_off):
    """data/models/ is committed, so this runs in CI. It is what `make gold` does offline."""
    resolved = tracking.resolve_model("binary")
    assert resolved.source == "committed"
    assert resolved.run_id is None
    assert resolved.booster is not None and resolved.calibrator is not None


def test_resolve_model_scores_identically_to_the_committed_pair(tracking_off):
    import pickle

    import xgboost

    from src.paths import MODEL_DIR
    from src.train import _prepare_X

    booster = xgboost.XGBClassifier()
    booster.load_model(MODEL_DIR / "binary_model.json")
    calibrator = pickle.loads((MODEL_DIR / "binary_calibrator.pkl").read_bytes())

    X = pd.DataFrame([{c: 0.0 for c in FEATURE_COLUMNS}])
    want_raw = booster.predict_proba(_prepare_X(X))[:, 1]
    got_raw, got_cal = tracking.resolve_model("binary").score(X)

    assert (got_raw == want_raw).all()
    assert (got_cal == calibrator.predict(want_raw)).all()


def test_a_missing_model_names_the_fix_instead_of_raising_from_xgboost(tracking_off, tmp_path):
    with pytest.raises(SystemExit, match="make final-models"):
        tracking.resolve_model("binary", model_dir=tmp_path)


# -- the params blob ---------------------------------------------------------------------------


def test_params_blob_covers_every_name_the_design_lists():
    """platform-design §5.3 names these explicitly. They live in four different modules, so a
    rename elsewhere silently drops one unless this is pinned."""
    blob = tracking.params_blob()
    for key in (
        "git_sha", "git_dirty", "feature_columns", "monotone_up", "n_splits", "random_state",
        "synthetic_dropout_fraction", "candidate_k", "max_edit_distance",
    ):
        assert key in blob, f"params_blob() lost {key}"
    assert any(k.startswith("tree_") for k in blob), "TREE_PARAMS is not being logged"


def test_params_blob_logs_the_feature_columns_in_order():
    """monotone_constraints_tuple() maps MONOTONE_UP onto FEATURE_COLUMNS *by index*, so the
    order is part of the model's definition (platform-design §4.3.3). Logging a sorted or
    set-ified list would record something that is not what the model was trained with."""
    assert tracking.params_blob()["feature_columns"] == list(FEATURE_COLUMNS)
    assert tracking.params_blob()["n_features"] == len(FEATURE_COLUMNS)


def test_params_blob_records_the_constrained_features():
    from src.train import MONOTONE_DOWN

    blob = tracking.params_blob()
    assert blob["monotone_up"] == sorted(MONOTONE_UP)
    assert blob["monotone_down"] == sorted(MONOTONE_DOWN)


def test_monotone_constraints_can_express_a_decreasing_feature():
    """The mechanism, not the constraint set. MONOTONE_DOWN is empty (rung v5 was ineligible —
    findings.md §10), but -1 has to be expressible: a feature like sim_rank_in_group is built
    with rank(ascending=False), so rank 1 is the *best* candidate and putting it in MONOTONE_UP
    would constrain it backwards. findings.md §4 and future-work.md both called this "a one-line
    change to MONOTONE_UP"; it never could have been."""
    from src.train import MONOTONE_DOWN, monotone_constraints_tuple

    constraints = dict(zip(FEATURE_COLUMNS, monotone_constraints_tuple()))
    assert constraints["jaro_winkler_full"] == 1
    assert constraints["sim_rank_in_group"] == 0, "v5 was not adopted"
    assert not (MONOTONE_UP & MONOTONE_DOWN)

    # The mechanism still emits -1 when asked to.
    import src.train as train

    original = train.MONOTONE_DOWN
    try:
        train.MONOTONE_DOWN = {"sim_rank_in_group"}
        probe = dict(zip(FEATURE_COLUMNS, train.monotone_constraints_tuple()))
        assert probe["sim_rank_in_group"] == -1
    finally:
        train.MONOTONE_DOWN = original


def test_monotone_constraints_reject_a_feature_that_does_not_exist():
    """A typo would otherwise constrain nothing at all, silently — the positional mapping just
    would not match it."""
    from src import train

    with pytest.raises(ValueError, match="not in feature_columns"):
        train.monotone_constraints_tuple(["some_other_column"])


def test_params_blob_import_direction_does_not_cycle():
    """train.py imports tracking; tracking's imports of train are function-local. If either moved
    to module level this would be an ImportError rather than a failure."""
    import importlib

    for module in ("src.tracking", "src.train"):
        importlib.reload(importlib.import_module(module))


# -- one vocabulary for the metric keys ----------------------------------------------------------


def test_objective_names_agree_across_call_sites():
    """The binary/rank x raw/calibrated cross product appears in five places with three different
    conventions. tracking.OBJECTIVES is the one source of truth they now derive from."""
    assert set(tracking.OBJECTIVES) == {"binary", "rank"}
    assert tracking.OBJECTIVES["binary"] == "binary:logistic"
    assert tracking.OBJECTIVES["rank"] == "rank:map"
    assert set(tracking.REGISTERED_MODEL) == set(tracking.OBJECTIVES)


def test_metric_keys_are_legal_and_distinguish_raw_from_calibrated():
    raw = tracking.metric_key("gold", "binary", "top1", "raw")
    cal = tracking.metric_key("gold", "binary", "top1", "calibrated")
    assert raw == "gold.binary.raw.top1"
    assert cal == "gold.binary.calibrated.top1"
    assert raw != cal, "findings.md §3 exists because these two differ; they cannot share a key"
    assert tracking.metric_key("oof", "rank", "reject_threshold") == "oof.rank.reject_threshold"


def test_gold_metrics_survive_a_threshold_no_row_clears(tracking_off):
    """Zero gold rows clear the OOF auto-accept threshold — that is the real result on this gold
    set for both objectives, so gold_threshold_check returns holds=None and NaN precision.
    Building the metric dict used to call float(None) and take the whole logging call down with a
    TypeError, silently leaving the champion run carrying stale numbers."""
    from src.evaluate import (
        gold_metrics,
        load_gold_features,
        load_oof_reference,
        score_gold_set,
    )

    result = score_gold_set(load_gold_features())
    reference = load_oof_reference()
    for objective in tracking.OBJECTIVES:
        metrics = gold_metrics(result, reference, objective)
        assert metrics[f"gold.{objective}.raw.top1"] > 0
        # Present-but-None would be just as fatal; the key must be absent entirely.
        assert f"gold.{objective}.accept_threshold_holds" not in metrics


def test_log_metrics_skips_nan_and_none(monkeypatch, tracking_on):
    """A NaN logged as a metric reads as a measurement. There wasn't one."""
    logged = {}

    class _FakeMlflow:
        def log_metric(self, key, value, step=None):
            logged[key] = value

    monkeypatch.setattr(tracking, "_mlflow", lambda: _FakeMlflow())
    # 'objective' is a real key in review_queue_reduction's dict and its value is a string.
    # Flattening that wholesale raised mid-loop and left the champion run half-rewritten.
    tracking.log_metrics(
        {"a": 1.0, "b": None, "c": float("nan"), "objective": "binary", "e": True, "d": 2.0}
    )
    assert set(logged) == {"a", "d"}


def test_oof_metrics_reports_both_score_kinds():
    """Ranking metrics use the raw score and thresholds use the calibrated one; a report that
    quotes one should be able to show the other (docs/findings.md §3)."""
    from src.train import oof_metrics

    oof = pd.DataFrame(
        {
            "wikidata_qid": ["Q1", "Q1", "Q2", "Q2"],
            "label": [1, 0, 0, 1],
            "binary_raw_score": [0.9, 0.1, 0.2, 0.8],
            "binary_calibrated_prob": [0.95, 0.05, 0.15, 0.85],
        }
    )
    metrics = oof_metrics(oof, "binary")
    assert "oof.binary.raw.top1" in metrics
    assert "oof.binary.calibrated.top1" in metrics
    assert "oof.binary.calibrated.brier" in metrics
