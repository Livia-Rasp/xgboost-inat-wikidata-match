"""Shared definitions for the three pipeline DAGs. Spec §7 milestone 16.

Three things live here because all three DAGs need them to agree:

**The assets.** `INGEST_COMPLETE` and `FEATURES` are the two the schedules key on; the four
artefact-level assets exist for lineage in the UI. A custom `x-matcher://` scheme, because
Airflow validates the pre-defined ones semantically (`file://` wants a real path on the machine
reading it, which these are not — they are paths inside a container's `data/` mount).

**The retry policy**, matched to the failure modes this project has actually hit rather than a
blanket `retries=3` (spec §7 milestone 16). `NETWORK_TASK` retries with exponential backoff;
`LOCAL_TASK` does not retry at all, because nothing about re-running deterministic local CPU work
makes it more likely to succeed.

**`guarded()`**, which is what makes those retries meaningful: a transient failure
(`wikidata.is_transient` — timeouts, dropped connections, 429/502/503/504, a truncated body, an
implausibly incomplete ancestor pull) propagates and Airflow retries it, while anything else is
re-raised as `AirflowFailException` so the task fails at once. Retrying a `KeyError` four times
with backoff wastes twenty minutes and teaches nobody anything.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from airflow.sdk import Asset, Variable
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from src.paths import file_fingerprint

# -- assets ------------------------------------------------------------------------------------

LOOKUP_CACHE = Asset(name="lookup_cache", uri="x-matcher://data/lookup.sqlite")
WIKIDATA_TAXA = Asset(name="wikidata_taxa", uri="x-matcher://data/wikidata_taxa.parquet")
WIKIDATA_ANCESTORS = Asset(name="wikidata_ancestors", uri="x-matcher://data/wikidata_ancestors.parquet")
CANDIDATES = Asset(name="candidates", uri="x-matcher://data/candidates.parquet")

# What `feature_build` waits for. One asset rather than the four above: an ingest run can change
# any subset of them, and a DAG scheduled on a list of assets waits for *all* of them to update
# while one scheduled on an OR would run once per event. A single "the ingest produced something
# new" asset says exactly what the downstream DAG cares about.
INGEST_COMPLETE = Asset(name="ingest_complete", uri="x-matcher://ingest")

FEATURES = Asset(name="features", uri="x-matcher://data/features.parquet")

# -- retry policy ------------------------------------------------------------------------------

NETWORK_TASK = {
    # WDQS needed four attempts across two upstream fixes to get one clean run (CLAUDE.md,
    # milestone 7), and its bad spells last minutes, not seconds. The client already retries
    # inside the request; this is the outer layer that survives losing a whole batch.
    "retries": 4,
    "retry_delay": timedelta(minutes=2),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=20),
    "execution_timeout": timedelta(hours=2),
}

LOCAL_TASK = {
    # Deterministic local work: a retry would reproduce the failure. The exception is OOM, which
    # a retry does not fix either.
    "retries": 0,
    "execution_timeout": timedelta(hours=2),
}

# DuckDB permits exactly one writing process per database file, and Cosmos runs one dbt process
# per model, so the dbt tasks must not overlap. The pool is created with one slot by the
# Terraform init job; `dags/` only references it.
DUCKDB_POOL = "duckdb"

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "dbt"


def guarded(fn, /, *args, **kwargs):
    """Run `fn`, classifying anything it raises into "retry" or "fail now"."""
    from src.wikidata import is_transient

    try:
        return fn(*args, **kwargs)
    except (AirflowSkipException, AirflowFailException):
        raise
    except Exception as exc:
        if is_transient(exc):
            raise
        raise AirflowFailException(f"{type(exc).__name__}: {exc}") from exc


# -- fingerprint-gated asset emission ----------------------------------------------------------


def _variable_key(asset: Asset) -> str:
    return f"matcher_fingerprint_{asset.name}"


def fingerprints(paths: dict[str, Path]) -> dict[str, str]:
    """Content fingerprints for the artefacts a stage wrote, or None where one is absent."""
    return {name: (file_fingerprint(path) if path.exists() else None) for name, path in paths.items()}


def publish_if_changed(asset: Asset, current: dict[str, str]) -> dict[str, str]:
    """Emit `asset`'s event only when the artefacts behind it actually changed.

    Airflow emits an asset event when a task with that outlet *succeeds*, and emits none when it
    is skipped — so skipping is how a task says "nothing new here". Without this, re-running
    ingest on a cache hit would retrain a model on byte-identical features, register a version and
    spend three minutes to reproduce the champion exactly (which is measured: it does reproduce).

    The previous fingerprint is kept in an Airflow Variable rather than read back off the last
    asset event, which keeps this independent of how Cosmos and Airflow construct event `extra`
    (astronomer-cosmos#2959) and readable in the UI under Admin → Variables.
    """
    key = _variable_key(asset)
    # deserialize_json to match how it is written below: without it the stored JSON comes back as
    # a string and never equals the dict, so every run would look like a change.
    previous = Variable.get(key, default=None, deserialize_json=True)
    if previous == current:
        raise AirflowSkipException(
            f"{asset.name} unchanged ({sorted(current)}) — not emitting an event, so nothing "
            "downstream re-runs on identical inputs"
        )
    Variable.set(key, current, serialize_json=True)
    return current
