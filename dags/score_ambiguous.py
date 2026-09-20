"""Score the checker's ambiguous queue with the champion, daily.

Spec §7 milestone 16: *"Reading the checker's ambiguous findings is in scope; writing anything
back to it is not."* This is the read half. The queue is the deployment distribution — the gold
set was drawn from it, and every number in the README describes how well the model ranks exactly
these items — so this is the one DAG whose output is about new taxa rather than about the model.

Daily rather than asset-triggered: its input is a database in *another* repository, written by a
tool this project does not run and cannot observe. Milestone 15's own rule applies — an absent
signal is not evidence of change — so it re-reads on a schedule instead of waiting for an event
that nothing here emits. A run with an empty queue costs one SQLite query.

The output is `data/scored_ambiguous.parquet`, a ranking and nothing more. `findings.md` §2
measured that the OOF-derived accept and reject thresholds do not transfer to this population, so
an accept/reject column would be a decision this project cannot yet justify; the QuickStatements
export that would act on one is milestone 10's, deliberately postponed.
"""

from __future__ import annotations

from airflow.sdk import DAG, task

from dags._common import NETWORK_TASK, guarded

DOC = __doc__

with DAG(
    dag_id="score_ambiguous",
    description="Rank the checker's ambiguous link findings with the registered champion",
    schedule="@daily",
    catchup=False,
    max_active_runs=1,
    tags=["matcher", "scoring"],
    doc_md=DOC,
) as dag:

    # NETWORK_TASK rather than LOCAL_TASK despite touching no network: it copies a database
    # another process writes, and a copy taken mid-write fails its integrity check. That is
    # transient in exactly the sense the retry policy means — the next attempt copies a different
    # moment — so it gets the same backoff as a WDQS blip.
    @task(**NETWORK_TASK)
    def read_findings() -> list[dict]:
        """The open ambiguous findings, from a local snapshot of the checker's database."""
        from src import ambiguous

        findings = guarded(ambiguous.load_findings)
        print(f"{len(findings):,} ambiguous item(s)")
        return findings.to_dict("records")

    @task(**NETWORK_TASK)
    def score(records: list[dict]) -> dict:
        """Attributes and ancestors from WDQS, candidates and features locally, then the champion.

        One task, with the network task's retries, because the WDQS pulls are the only part that
        can fail transiently and splitting them off would mean passing a feature frame through
        XCom.
        """
        import pandas as pd

        from src import ambiguous

        if not records:
            print("nothing in the queue — no scoring, no output")
            return {"items": 0, "rows": 0}

        findings = pd.DataFrame.from_records(records)
        features = guarded(ambiguous.build_scoring_features, findings)
        if features.empty:
            print("candidate generation returned nothing for these items")
            return {"items": len(findings), "rows": 0}

        scored = ambiguous.score(features)
        path = ambiguous.write_output(scored, findings)
        print(f"{len(scored):,} rows for {scored['wikidata_qid'].nunique():,} items -> {path}")
        return {"items": int(scored["wikidata_qid"].nunique()), "rows": int(len(scored))}

    score(read_findings())
