"""Train a challenger, score it against the champion, and promote or hold.

Spec §7 milestone 16: *"Promotion of a newly trained model is gated on a comparison against the
registered champion, which is the difference between a DAG and a shell script."* The rule itself
is `src/promote.py` (`docs/findings.md` §10); this DAG is only its schedule and its outcomes.

Asset-triggered on `features`, and manually triggerable — a change to `train.py` or to a feature
definition that leaves the feature table byte-identical produces no asset event, and that is the
case where a human presses the button.

    promote   the champion alias moves, data/models/ and data/oof_predictions.* are rewritten
              (a git diff for a human to review and commit)
    hold      registered under the `challenger` alias, champion untouched — a green run
    regress   `assert_not_regressed` fails, so the run is red and means something
"""

from __future__ import annotations

from airflow.sdk import DAG, TriggerRule, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from dags._common import FEATURES, LOCAL_TASK

DOC = __doc__

with DAG(
    dag_id="train_and_evaluate",
    description="OOF + refit, gold scoring against the champion, and the promotion gate",
    schedule=FEATURES,
    catchup=False,
    max_active_runs=1,
    tags=["matcher", "train", "mlflow"],
    doc_md=DOC,
) as dag:

    @task(**LOCAL_TASK)
    def train_challenger(**context) -> str:
        """5-fold OOF for both objectives, then refit both on everything.

        Into a fresh `data/runs/<dag_run_id>/`: the OOF cache is keyed on the feature table's
        contents and the hyperparameters, *not* on the code, so a shared directory would hand back
        the previous run's predictions for exactly the code-only change this DAG exists to measure.
        Named after the DAG run so a directory can be traced back to the run that wrote it.
        """
        from src import promote

        run_dir = promote.new_run_dir(context["dag_run"].run_id.replace(":", "").replace("+", ""))
        promote.train(run_dir)
        return str(run_dir)

    @task(**LOCAL_TASK)
    def gate(run_dir: str, **context) -> dict:
        """Score both pairs on the current gold set, log to MLflow, register, decide.

        The MLflow run is tagged with the Airflow ids, which is the link between an experiment and
        the pipeline run that produced it.
        """
        from pathlib import Path

        from src import promote

        dag_run = context["dag_run"]
        return promote.evaluate_and_register(
            Path(run_dir),
            tags={
                "airflow_dag_id": dag_run.dag_id,
                "airflow_run_id": dag_run.run_id,
                # .value, not str(): run_type is a DagRunType enum, and str() tags the run
                # "DagRunType.MANUAL" — which also made an == "asset_triggered" test never match.
                "trigger": getattr(dag_run.run_type, "value", str(dag_run.run_type)),
            },
        )

    @task(**LOCAL_TASK)
    def promote_champion(summary: dict) -> str:
        """Move the alias and rewrite the committed export — only when the rule said promote."""
        from pathlib import Path

        from src import promote

        if summary["outcome"] != promote.PROMOTE:
            raise AirflowSkipException(f"verdict was {summary['outcome']}; the champion stays")
        promote.promote(Path(summary["run_dir"]), summary["versions"])
        return summary["versions"]["rank"]

    @task(trigger_rule=TriggerRule.ALL_DONE, **LOCAL_TASK)
    def assert_not_regressed(summary: dict) -> None:
        """The red light. Separate from `gate` so that a challenger being *measured* as worse is
        still a task that did its job: the metrics are logged and the version is registered either
        way, and only this task fails. ALL_DONE because `promote_champion` skips on hold.
        """
        from src import promote

        if summary["outcome"] == promote.REGRESS:
            raise AirflowFailException(
                "challenger regressed against the champion:\n  " + "\n  ".join(summary["reasons"])
            )
        print(f"verdict {summary['outcome']}: no regression")

    summary = gate(train_challenger())
    promote_champion(summary) >> assert_not_regressed(summary)
