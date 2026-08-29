"""RANK_LEVEL as a table — the ordinal rank scale rank_level_diff is measured on.

Keyed by rank *name*, so it serves both sides: the Wikidata side arrives through
stg_rank_names.rank_level, the iNat side joins here directly on its own `rank` string.
Reused from wikidata-inat-checker's RANK_ORDER; see src/labels.py.
"""

import pandas as pd

from src.labels import RANK_LEVEL


def model(dbt, session):
    dbt.config(materialized="table")
    return pd.DataFrame(
        [{"rank_name": name, "rank_level": level} for name, level in RANK_LEVEL.items()]
    )
