"""Features: the dbt project, the pandas comparand, and the parity report.

Spec §7 milestone 16. Asset-triggered on `ingest_complete`, so a deliberate ingest run flows
through to features with nobody watching.

Cosmos renders one Airflow task per dbt model (`TestBehavior.BUILD`, so each model's tests run in
the same task — which is what `make features-sql`'s `dbt build` does), and every one of them goes
through a **one-slot pool**: DuckDB allows a single writing process per database file, and these
tasks are separate dbt processes over `data/warehouse.duckdb`. Without the pool they race and fail
with `Could not set lock on file`.
"""

from __future__ import annotations

import os

from airflow.sdk import DAG, task
from cosmos import (
    DbtTaskGroup,
    ExecutionConfig,
    ExecutionMode,
    LoadMode,
    ProfileConfig,
    ProjectConfig,
    RenderConfig,
    TestBehavior,
)

from dags._common import (
    DBT_PROJECT_DIR,
    DUCKDB_POOL,
    FEATURES,
    INGEST_COMPLETE,
    LOCAL_TASK,
    fingerprints,
    guarded,
    publish_if_changed,
)

DOC = __doc__

# profiles_yml_filepath, not a Cosmos profile mapping: dbt/profiles.yml is checked in, derives
# every path from MATCHER_DATA_DIR, attaches lookup.sqlite read-only and registers the
# normalize_name() UDF plugin. A mapping would be a second, drifting description of all that.
profile_config = ProfileConfig(
    profile_name="matcher",
    target_name="dev",
    profiles_yml_filepath=DBT_PROJECT_DIR / "profiles.yml",
)

# Cosmos hands dbt a filtered environment, so anything dbt's own `env_var()` reads has to be
# forwarded explicitly. Without this, `MATCHER_DATA_DIR` falls back to its default — the relative
# `data/` — which resolves against dbt's working directory rather than the repo root and fails
# with `IO Error: No files found that match the pattern "data/features.parquet"`. The same trap
# CLAUDE.md records for running dbt by hand from the wrong directory, arriving by another route.
DBT_ENV_VARS = {
    name: os.environ[name]
    for name in ("MATCHER_DATA_DIR", "MATCHER_TAXA_DB", "DBT_LOG_PATH", "DBT_TARGET_PATH")
    if name in os.environ
}

with DAG(
    dag_id="feature_build",
    description="dbt build over DuckDB, the pandas comparand, and the parity report",
    schedule=INGEST_COMPLETE,
    catchup=False,
    max_active_runs=1,
    tags=["matcher", "features", "dbt"],
    doc_md=DOC,
) as dag:

    dbt_features = DbtTaskGroup(
        group_id="dbt",
        # install_dbt_deps=False: this project deliberately has no dbt packages (the `between`
        # test is a local macro precisely so `dbt deps` and a network fetch are never needed), so
        # there is nothing to install and a deps step would only add a failure mode.
        project_config=ProjectConfig(
            dbt_project_path=DBT_PROJECT_DIR, install_dbt_deps=False, env_vars=DBT_ENV_VARS
        ),
        profile_config=profile_config,
        execution_config=ExecutionConfig(execution_mode=ExecutionMode.LOCAL),
        render_config=RenderConfig(
            # dbt ls at DAG-parse time, which was verified to work against a completely empty
            # data/ — it never opens the database — so parsing does not depend on a built
            # lookup.sqlite. Cosmos caches the result.
            load_method=LoadMode.DBT_LS,
            # All tests in one task after every model, rather than each model's tests inside its
            # own task (TestBehavior.BUILD). BUILD looks closer to `make features-sql`, but it
            # attaches each test to *one* model, and `assert_recall_ceiling` references two
            # (fct_features and stg_inat_taxa) — so it ran with the earlier of them, before the
            # table it reads existed. `dbt build` orders that correctly; Cosmos' per-model split
            # cannot. AFTER_ALL is also what platform-design §5.4's table described.
            test_behavior=TestBehavior.AFTER_ALL,
            # Cosmos' own asset emission is derived from OpenLineage artefacts in this execution
            # mode and can silently emit nothing (astronomer-cosmos#2959). The features asset is
            # emitted by publish_features() below instead.
            emit_datasets=False,
        ),
        operator_args={"pool": DUCKDB_POOL},
        default_args=LOCAL_TASK,
    )

    @task(**LOCAL_TASK)
    def pandas_features() -> int:
        """The same table built in pandas, into `features_pandas.parquet`.

        Kept as the parity comparand, not as the model's input: milestone 15 promoted the dbt
        table to the canonical path. `force_refresh=True` because this manifest fingerprints its
        *inputs*, not the code that reads them, so a feature-definition edit leaves the cache
        looking valid — the exact trap CLAUDE.md records from the milestone 15 alignment. A DAG
        run that silently reused an old pandas table would report false parity.
        """
        import pandas as pd

        from src.candidates import DEFAULT_CANDIDATES_PATH, build_lookup_cache
        from src.features import LOOKUP_SQLITE_PATH, _load_inat_index, build_features_and_splits
        from src.wikidata import DEFAULT_ANCESTORS_CACHE_PATH
        from src.wikidata import DEFAULT_CACHE_PATH as WIKIDATA_TAXA_PATH

        build_lookup_cache().close()
        df = guarded(
            build_features_and_splits,
            pd.read_parquet(DEFAULT_CANDIDATES_PATH),
            pd.read_parquet(WIKIDATA_TAXA_PATH),
            pd.read_parquet(DEFAULT_ANCESTORS_CACHE_PATH),
            _load_inat_index(),
            source_paths={
                "candidates": DEFAULT_CANDIDATES_PATH,
                "wikidata_taxa": WIKIDATA_TAXA_PATH,
                "ancestors": DEFAULT_ANCESTORS_CACHE_PATH,
                "lookup_sqlite": LOOKUP_SQLITE_PATH,
            },
            force_refresh=True,
        )
        print(f"{len(df):,} pandas feature rows, {df.shape[1]} columns")
        return len(df)

    @task(**LOCAL_TASK)
    def parity_report() -> dict:
        """Milestone 14's column-by-column diff, as a task rather than a footnote.

        Reported, not enforced: the two paths agree on all 52 columns to within one ULP on three
        Jaro-Winkler columns (milestone 15), and a real divergence is something to read, not
        something to fail a training run over. The numbers land in the task log.
        """
        import build_parity_report

        pandas_frame, dbt_frame = build_parity_report.load_frames()
        table = build_parity_report.parity_table(pandas_frame, dbt_frame)
        differing = table[table["rows_differing"] > 0]
        for row in differing.itertuples():
            print(f"  {row.column}: {row.rows_differing:,} rows differ ({row.share:.2%}, "
                  f"max delta {row.max_abs_delta:g})")
        print(f"{len(table) - len(differing)}/{len(table)} columns identical")
        return {"columns": int(len(table)), "differing": int(len(differing))}

    @task(outlets=[FEATURES], **LOCAL_TASK)
    def publish_features() -> dict:
        """Emit the features asset — and only on a real change, so an ingest that reproduced its
        own bytes does not trigger a retrain that reproduces the champion."""
        from src.features import DEFAULT_FEATURES_PATH

        return publish_if_changed(FEATURES, fingerprints({"features": DEFAULT_FEATURES_PATH}))

    dbt_features >> pandas_features() >> parity_report() >> publish_features()
