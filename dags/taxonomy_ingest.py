"""Ingest: the iNat lookup index, the Wikidata pull, its ancestor chains, and candidates.

Spec §7 milestone 16. **Triggered by hand, not on a schedule** — see `docs/platform-design.md`
§5.4 amendment 1. The Wikidata pull has no staleness check by design (it must not silently
re-hit a shared public endpoint), so a scheduled run would either do nothing or force a re-pull
that changes the training population; and a retrain that moves code *and* data at once cannot
attribute what changed. `force_refresh` is therefore a parameter of a deliberate run.

    Trigger DAG → force_refresh: true     # re-pull WDQS and regenerate everything below it

Everything downstream stays asset-triggered: this DAG ends by emitting `ingest_complete`, and
only when something actually changed.
"""

from __future__ import annotations

from airflow.sdk import DAG, Param, task

from dags._common import (
    CANDIDATES,
    INGEST_COMPLETE,
    LOCAL_TASK,
    LOOKUP_CACHE,
    NETWORK_TASK,
    WIKIDATA_ANCESTORS,
    WIKIDATA_TAXA,
    fingerprints,
    guarded,
    publish_if_changed,
)

DOC = __doc__

with DAG(
    dag_id="taxonomy_ingest",
    description="Lookup index, Wikidata pull, ancestor chains, candidate generation",
    schedule=None,
    catchup=False,
    max_active_runs=1,
    params={
        "force_refresh": Param(
            False,
            type="boolean",
            title="Force a fresh Wikidata pull",
            description="Re-query WDQS and rebuild everything downstream, instead of reusing the "
            "cached pull. ~30 batched SPARQL requests plus ~8 minutes of ancestor chains.",
        )
    },
    tags=["matcher", "ingest"],
    doc_md=DOC,
) as dag:

    @task(**LOCAL_TASK)
    def lookup_cache() -> str:
        """The normalised-name + FTS5 trigram index over the sibling repo's taxa.db (milestone 1).

        Local work over a 1.4M-row SQLite file: no retries, and it rebuilds itself only when
        taxa.db's contents or the schema version change.
        """
        from src import candidates

        conn = guarded(candidates.build_lookup_cache)
        conn.close()
        return str(candidates.DEFAULT_CACHE_PATH)

    @task(**NETWORK_TASK)
    def wikidata_pull(**context) -> int:
        """Milestone 2's batched SPARQL pull of taxa carrying P3151."""
        from src import wikidata

        force = bool(context["params"]["force_refresh"])
        result = guarded(wikidata.build_pull_cache, force_refresh=force)
        print(f"{len(result.taxa):,} taxa ({'cache hit' if result.cache_hit else 'fresh pull'})")
        return len(result.taxa)

    @task(**NETWORK_TASK)
    def ancestor_pull(**context) -> int:
        """Transitive P171 chains (milestone 4's input).

        The one WDQS query that can come back HTTP 200 with a silently incomplete body. The
        client retries low coverage in-process and now raises `TransientSourceError` when that
        does not clear, which is what makes this task's retries do something other than cache a
        partial pull.
        """
        import pandas as pd

        from src import wikidata

        qids = pd.read_parquet(wikidata.DEFAULT_CACHE_PATH)["qid"].tolist()
        ancestors = guarded(
            wikidata.build_ancestor_chains, qids, force_refresh=bool(context["params"]["force_refresh"])
        )
        print(f"{len(ancestors):,} ancestor rows for {ancestors['qid'].nunique():,} items")
        return len(ancestors)

    @task(**LOCAL_TASK)
    def generate_candidates(**context) -> int:
        """Milestone 3's five strategies, K=20, parallel across MATCHER_WORKERS processes."""
        import pandas as pd

        from src import candidates as candidates_module
        from src import wikidata

        wd = pd.read_parquet(wikidata.DEFAULT_CACHE_PATH)
        rows = guarded(
            candidates_module.build_candidates_cache,
            wd,
            wikidata_parquet_path=wikidata.DEFAULT_CACHE_PATH,
            force_refresh=bool(context["params"]["force_refresh"]),
        )
        print(f"{len(rows):,} candidate rows for {wd['qid'].nunique():,} items")
        return len(rows)

    @task(
        # The four artefact assets are emitted for lineage; `ingest_complete` is the one
        # `feature_build` is scheduled on.
        outlets=[INGEST_COMPLETE, LOOKUP_CACHE, WIKIDATA_TAXA, WIKIDATA_ANCESTORS, CANDIDATES],
        **LOCAL_TASK,
    )
    def publish() -> dict:
        """Emit the ingest assets, but only if the artefacts differ from the last published run.

        The gate sits *here* rather than on each stage on purpose: a stage that skipped would skip
        everything downstream of it under the default trigger rule, and an ingest where only the
        Wikidata pull moved still has to reach candidate generation.
        """
        from src import candidates, wikidata

        current = fingerprints({
            "lookup_sqlite": candidates.DEFAULT_CACHE_PATH,
            "wikidata_taxa": wikidata.DEFAULT_CACHE_PATH,
            "wikidata_ancestors": wikidata.DEFAULT_ANCESTORS_CACHE_PATH,
            "candidates": candidates.DEFAULT_CANDIDATES_PATH,
        })
        return publish_if_changed(INGEST_COMPLETE, current)

    lookup = lookup_cache()
    taxa = wikidata_pull()
    ancestors = ancestor_pull()
    generated = generate_candidates()

    taxa >> ancestors
    [lookup, taxa] >> generated
    [generated, ancestors] >> publish()
