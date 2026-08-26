"""Committed fallbacks for the gold-set evaluation, so it runs from a clean clone.

Everything in `data/` is gitignored (spec §0) and most of it is far too large to commit: the iNat
lookup index alone is 493 MB. But the gold-set path only ever touches a few thousand of those
rows, and `gold/hard_cases.csv` — the actual deliverable — is already committed. Extending that
to the handful of tables it needs makes `python -m src.evaluate --gold` reproducible without the
sibling repo, Node, or a 189 MB download.

The fixtures are gzipped CSV rather than parquet on purpose: they are small enough that the
format costs nothing, and a reviewer can read them. `build_fixtures.py` regenerates all of them
from the full caches, so they are derived artifacts with a command behind them, not blobs.

Real caches always win. These only load when `data/` has nothing to offer, and every loader says
which mode it used, because silently scoring against a 4,000-row index when you meant to score
against the real one is exactly the kind of thing that should not be quiet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .paths import FIXTURE_DIR

GOLD_INAT_INDEX_FIXTURE = FIXTURE_DIR / "gold_inat_index.csv.gz"
GOLD_ATTRIBUTES_FIXTURE = FIXTURE_DIR / "gold_wikidata_attributes.csv.gz"
GOLD_ANCESTORS_FIXTURE = FIXTURE_DIR / "gold_wikidata_ancestors.csv.gz"
OOF_SUMMARY_FIXTURE = FIXTURE_DIR / "oof_summary.json"

_announced: set[str] = set()


def announce(what: str, source: Path) -> None:
    """Say once per process which copy of a table is in use."""
    if what not in _announced:
        _announced.add(what)
        print(f"[fixture] {what}: {source}")


def read_csv_fixture(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Either build the full caches (see README, 'The full path') or "
            f"regenerate the fixtures with `python build_fixtures.py`."
        )
    return pd.read_csv(path, **kwargs)
