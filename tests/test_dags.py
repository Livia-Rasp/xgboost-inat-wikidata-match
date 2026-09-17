"""The DAGs parse, and their retry policy is the one the design argued for. Spec §7 milestone 16.

Airflow is not in the `dev` extra — it lives in the Airflow image — so this module skips on the
host and runs for real in CI's docker job. The properties pinned here are the ones that are easy
to break silently: a DAG that stops importing is invisible in the UI rather than loud, a network
task that loses its retries only shows up the next time WDQS has a bad afternoon, and a dbt task
that escapes the one-slot pool fails with a DuckDB lock error that reads like a dbt bug.

The DAG modules are imported directly rather than through `DagBag`: in Airflow 3.3 `get_dag()`
reads the metadata database, and whether a file *parses* is not a question that should need one.
An import failure surfaces here as the exception it is.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

pytest.importorskip("airflow", reason="Airflow lives in the Airflow image, not the dev extra")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED = ("taxonomy_ingest", "feature_build", "train_and_evaluate")

# Which tasks are allowed to retry, per DAG. Everything else must not (spec §7 milestone 16 asks
# for retries matched to real failure modes, explicitly not a blanket retries=3).
NETWORK_TASKS = {"taxonomy_ingest": {"wikidata_pull", "ancestor_pull"}}


@pytest.fixture(scope="module")
def dags(tmp_path_factory):
    """Every DAG object, by dag_id, parsed against a throwaway data directory."""
    # Set, not setdefault: whatever the surrounding environment points these at is not necessarily
    # writable, and dbt insists on writing logs and target/ somewhere — by default inside the
    # project directory, which is mounted read-only in the containers.
    data_dir = tmp_path_factory.mktemp("data")
    os.environ["MATCHER_DATA_DIR"] = str(data_dir)
    os.environ["DBT_LOG_PATH"] = str(data_dir / "logs")
    os.environ["DBT_TARGET_PATH"] = str(data_dir / "target")
    # Cosmos caches its dbt ls output in an Airflow Variable, which needs the metadata database.
    # Parsing must not, so the cache is off here and left on in the deployed containers.
    os.environ["AIRFLOW__COSMOS__ENABLE_CACHE"] = "False"

    parsed = {}
    for name in EXPECTED:
        module = importlib.import_module(f"dags.{name}")
        dag = module.dag
        # The file name and the dag_id must agree, or the UI and the repo disagree about what
        # a DAG is called.
        assert dag.dag_id == name
        parsed[name] = dag
    return parsed


def test_dbt_ls_at_parse_time_needs_no_built_data(dags):
    """The whole fixture is the assertion: Cosmos renders the dbt task group by running `dbt ls`
    while parsing, against a data directory that holds nothing at all. If that ever starts opening
    the DuckDB database, every DAG in the folder stops parsing on a fresh machine."""
    assert set(dags) == set(EXPECTED)


def test_ingest_is_manual_and_the_rest_are_asset_triggered(dags):
    """platform-design §5.4 amendment 1: data moves when a human decides it does; everything
    downstream of ingest runs on its own."""
    assert dags["taxonomy_ingest"].schedule is None
    for dag_id in ("feature_build", "train_and_evaluate"):
        assert dags[dag_id].schedule is not None


def test_ingest_task_graph(dags):
    dag = dags["taxonomy_ingest"]
    assert set(dag.task_ids) == {
        "lookup_cache", "wikidata_pull", "ancestor_pull", "generate_candidates", "publish",
    }
    # Candidate generation needs both the index and the pull; ancestors need the pull's QIDs.
    assert dag.get_task("generate_candidates").upstream_task_ids == {"lookup_cache", "wikidata_pull"}
    assert dag.get_task("ancestor_pull").upstream_task_ids == {"wikidata_pull"}
    assert dag.get_task("publish").upstream_task_ids == {"generate_candidates", "ancestor_pull"}


def test_only_the_network_tasks_retry(dags):
    for dag_id, dag in dags.items():
        for task in dag.tasks:
            expected = task.task_id in NETWORK_TASKS.get(dag_id, set())
            assert (task.retries > 0) is expected, f"{dag_id}.{task.task_id} retries={task.retries}"
            if expected:
                assert task.retry_exponential_backoff
                assert task.max_retry_delay is not None


def test_dbt_tasks_are_all_in_the_one_slot_pool(dags):
    """DuckDB permits one writing process per file; Cosmos runs one dbt process per model."""
    from dags._common import DUCKDB_POOL

    dbt_tasks = [t for t in dags["feature_build"].tasks if t.task_id.startswith("dbt.")]
    assert len(dbt_tasks) >= 14, f"expected the dbt models to render as tasks, got {len(dbt_tasks)}"
    assert {t.pool for t in dbt_tasks} == {DUCKDB_POOL}


def test_the_features_asset_is_ours_not_cosmos(dags):
    """Cosmos' own emission rides on OpenLineage parsing and can silently emit nothing
    (astronomer-cosmos#2959), so the trigger for retraining must not depend on it."""
    from dags._common import FEATURES

    dag = dags["feature_build"]
    assert [a.name for a in dag.get_task("publish_features").outlets] == [FEATURES.name]
    for task in dag.tasks:
        if task.task_id.startswith("dbt."):
            assert not task.outlets, f"{task.task_id} emits {task.outlets}"


def test_ingest_publishes_every_artefact_asset(dags):
    """The four artefact assets are lineage; ingest_complete is what feature_build waits on."""
    from dags._common import CANDIDATES, INGEST_COMPLETE, LOOKUP_CACHE, WIKIDATA_ANCESTORS, WIKIDATA_TAXA

    outlets = {a.name for a in dags["taxonomy_ingest"].get_task("publish").outlets}
    assert outlets == {a.name for a in (INGEST_COMPLETE, LOOKUP_CACHE, WIKIDATA_TAXA,
                                        WIKIDATA_ANCESTORS, CANDIDATES)}


def test_the_gate_and_its_outcomes(dags):
    dag = dags["train_and_evaluate"]
    assert set(dag.task_ids) == {"train_challenger", "gate", "promote_champion", "assert_not_regressed"}
    assert dag.get_task("gate").upstream_task_ids == {"train_challenger"}
    # ALL_DONE, because promote_champion skips on a hold and the red light must still run.
    assert dag.get_task("assert_not_regressed").trigger_rule == "all_done"
