"""Candidate generation from the SQLite taxa index. See docs/inat-wikidata-match-spec.md §2.

This module currently covers milestone 1 (ingest): read-only access to the iNat taxa index and
the normalised-name / trigram lookup tables it's built into. The candidate-generation strategies
themselves (milestone 3) are not implemented yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .normalize import normalize_name

DEFAULT_TAXA_DB_PATH = Path.home() / ".cache" / "wikidata-inat-checker" / "taxa.db"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "lookup.sqlite"


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def build_lookup_cache(
    taxa_db_path: Path = DEFAULT_TAXA_DB_PATH,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> sqlite3.Connection:
    """Return a connection to the normalised-name + trigram lookup cache, rebuilding it from
    taxa_db_path if it's missing or older than the source db."""
    if cache_path.exists() and cache_path.stat().st_mtime >= taxa_db_path.stat().st_mtime:
        return sqlite3.connect(cache_path)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.unlink(missing_ok=True)

    source = connect_readonly(taxa_db_path)
    cache = sqlite3.connect(cache_path)
    try:
        cache.execute(
            """
            CREATE TABLE taxa_normalized (
                taxon_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rank TEXT NOT NULL,
                ancestry TEXT,
                normalized_name TEXT NOT NULL
            )
            """
        )
        cache.execute(
            "CREATE INDEX idx_normalized_name ON taxa_normalized(normalized_name)"
        )
        cache.execute(
            """
            CREATE VIRTUAL TABLE taxa_trigram USING fts5(
                normalized_name,
                taxon_id UNINDEXED,
                tokenize='trigram'
            )
            """
        )

        rows = source.execute("SELECT taxon_id, name, rank, ancestry FROM taxa")
        batch = []
        for taxon_id, name, rank, ancestry in rows:
            normalized_name = normalize_name(name).normalized
            batch.append((taxon_id, name, rank, ancestry, normalized_name))
            if len(batch) >= 10_000:
                _flush_batch(cache, batch)
                batch = []
        if batch:
            _flush_batch(cache, batch)

        cache.commit()
    finally:
        source.close()

    return cache


def _flush_batch(cache: sqlite3.Connection, batch: list[tuple]) -> None:
    cache.executemany(
        "INSERT INTO taxa_normalized VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    cache.executemany(
        "INSERT INTO taxa_trigram (normalized_name, taxon_id) VALUES (?, ?)",
        [(row[4], row[0]) for row in batch],
    )


def lookup_by_normalized_name(cache: sqlite3.Connection, name: str) -> list[dict]:
    query = normalize_name(name).normalized
    cursor = cache.execute(
        "SELECT taxon_id, name, rank, ancestry FROM taxa_normalized WHERE normalized_name = ?",
        (query,),
    )
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


if __name__ == "__main__":
    conn = build_lookup_cache()
    matches = lookup_by_normalized_name(conn, "prunella")
    print(f"{len(matches)} match(es) for 'prunella':")
    for row in matches:
        print(f"  {row}")
