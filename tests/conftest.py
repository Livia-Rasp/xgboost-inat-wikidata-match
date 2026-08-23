"""Shared fixtures.

The candidate-generation tests need a real SQLite index, not a mock: the strategies are SQL, one
of them is an FTS5 trigram query, and mocking that would only test the mock. So the suite builds
a genuine index the same way production does — `candidates.build_lookup_cache()` against a
throwaway `taxa.db` — just from 26 hand-written rows instead of 1.4 million.

The rows live in `tests/fixtures/taxa_mini.csv` and are edited by hand on purpose. A fixture
derived from the live iNat dump would stop being a fixed point the moment that dump refreshed,
which is the failure this project has already been bitten by once (see CLAUDE.md on
re-executing the report notebook).
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
TAXA_MINI_CSV = FIXTURE_DIR / "taxa_mini.csv"


@pytest.fixture(scope="session")
def taxa_db(tmp_path_factory) -> Path:
    """A throwaway taxa.db in the same 4-column schema wikidata-inat-checker produces."""
    path = tmp_path_factory.mktemp("taxa") / "taxa.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE taxa (
                taxon_id TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                rank     TEXT NOT NULL,
                ancestry TEXT
            )
            """
        )
        with TAXA_MINI_CSV.open(newline="") as handle:
            rows = [
                (row["taxon_id"], row["name"], row["rank"], row["ancestry"] or None)
                for row in csv.DictReader(handle)
            ]
        conn.executemany("INSERT INTO taxa VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def lookup(taxa_db, tmp_path_factory) -> sqlite3.Connection:
    """The normalised-name + trigram cache, built by the real production function."""
    from src.candidates import build_lookup_cache

    cache_path = tmp_path_factory.mktemp("lookup") / "lookup.sqlite"
    return build_lookup_cache(taxa_db_path=taxa_db, cache_path=cache_path)


@pytest.fixture(scope="session")
def inat_index(lookup):
    """The same frame features.build_features() consumes, read out of the fixture index."""
    import pandas as pd

    return pd.read_sql_query(
        "SELECT taxon_id, name, rank, ancestry, genus, specific_epithet FROM taxa_normalized",
        lookup,
    )
