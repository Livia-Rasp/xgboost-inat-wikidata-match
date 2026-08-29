"""WD_RANK_TO_NAME as a table.

A dbt seed CSV would be the conventional home for a 27-row lookup, but this one was derived
empirically (labels.py's docstring records how) and is consumed by the pandas path too. A copy in
seeds/ could drift from it silently, and the parity report would then be measuring the copy.
"""

import pandas as pd

from src.labels import RANK_LEVEL, WD_RANK_TO_NAME


def model(dbt, session):
    dbt.config(materialized="table")
    return pd.DataFrame(
        [
            {"rank_qid": qid, "rank_name": name, "rank_level": RANK_LEVEL.get(name)}
            for qid, name in WD_RANK_TO_NAME.items()
        ]
    )
