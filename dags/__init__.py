"""Airflow DAGs for the pipeline. Spec §7 milestone 16.

A package rather than three loose files so `dags._common` is one import shared by all three DAGs.
That needs the repository root on `sys.path` — Airflow puts the *dags folder* there, not its
parent — which is what `PYTHONPATH=/opt/project` in the Airflow containers provides, alongside
making `src` importable from the bind-mounted checkout.
"""
