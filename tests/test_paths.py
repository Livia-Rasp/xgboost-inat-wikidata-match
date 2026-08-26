"""Regression tests for the three things that made this code container-hostile.

All three are silent failure modes rather than crashes a green suite would notice on its own:
a cache that rebuilds when it should not, a pool that oversubscribes, and — the one real bug —
a staleness check that raised FileNotFoundError when asked about a source that is legitimately
absent. Spec §7 milestone 13.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pandas as pd
import pytest

from src import candidates
from src.paths import file_fingerprint, worker_count

# -- the lookup cache must survive an absent taxa.db -------------------------------------------


def test_cache_is_valid_when_the_source_db_is_absent(taxa_db, tmp_path):
    """A container that mounts a prebuilt lookup.sqlite but not the sibling repo's taxa.db is a
    supported setup — the five-minute path never needs the source. This used to raise
    FileNotFoundError from inside a predicate."""
    # Build a real cache, then ask about it with a source path that does not exist.
    cache_path = tmp_path / "lookup.sqlite"
    candidates.build_lookup_cache(taxa_db_path=taxa_db, cache_path=cache_path).close()

    missing_source = tmp_path / "definitely-not-here" / "taxa.db"
    assert not missing_source.exists()
    assert candidates._cache_is_valid(cache_path, missing_source) is True


def test_build_lookup_cache_reuses_the_cache_without_the_source(taxa_db, tmp_path):
    cache_path = tmp_path / "lookup.sqlite"
    candidates.build_lookup_cache(taxa_db_path=taxa_db, cache_path=cache_path).close()

    conn = candidates.build_lookup_cache(
        taxa_db_path=tmp_path / "gone" / "taxa.db", cache_path=cache_path
    )
    try:
        n = conn.execute("SELECT count(*) FROM taxa_normalized").fetchone()[0]
    finally:
        conn.close()
    assert n > 0


def test_build_lookup_cache_explains_itself_when_it_can_build_nothing(tmp_path):
    """No cache and no source is the one case that genuinely cannot proceed. It should name the
    mount rather than surfacing a bare FileNotFoundError from sqlite."""
    with pytest.raises(SystemExit) as excinfo:
        candidates.build_lookup_cache(
            taxa_db_path=tmp_path / "gone" / "taxa.db", cache_path=tmp_path / "absent.sqlite"
        )
    assert "MATCHER_TAXA_DB" in str(excinfo.value)


# -- the candidates manifest must key on content, not mtime ------------------------------------


def _tiny_wikidata_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [{"qid": "Q1", "name": "Prunella modularis", "synonym_names": [], "basionym_names": []}]
    )


def test_candidates_cache_hits_after_the_source_mtime_moves(taxa_db, tmp_path):
    """The regression this milestone exists for. Copying a file into an image layer, restoring a
    volume or checking out a fresh clone all rewrite mtimes without touching content; the old
    manifest treated that as a reason to regenerate 590k rows."""
    lookup_path = tmp_path / "lookup.sqlite"
    candidates.build_lookup_cache(taxa_db_path=taxa_db, cache_path=lookup_path).close()

    parquet = tmp_path / "wikidata_taxa.parquet"
    _tiny_wikidata_frame().to_parquet(parquet, index=False)

    kwargs = dict(
        candidates_path=tmp_path / "candidates.parquet",
        manifest_path=tmp_path / "candidates.manifest.json",
        lookup_sqlite_path=lookup_path,
        wikidata_parquet_path=parquet,
        processes=1,
    )
    first = candidates.build_candidates_cache(_tiny_wikidata_frame(), **kwargs)

    before = json.loads((tmp_path / "candidates.manifest.json").read_text())
    os.utime(lookup_path, (1, 1))
    os.utime(parquet, (1, 1))

    second = candidates.build_candidates_cache(_tiny_wikidata_frame(), **kwargs)
    after = json.loads((tmp_path / "candidates.manifest.json").read_text())

    assert before == after, "a pure mtime change must not rewrite the manifest"
    pd.testing.assert_frame_equal(first, second)


def test_candidates_cache_misses_when_a_source_actually_changes(taxa_db, tmp_path):
    """The other half: content-keyed must still mean *keyed*, not ignored."""
    lookup_path = tmp_path / "lookup.sqlite"
    candidates.build_lookup_cache(taxa_db_path=taxa_db, cache_path=lookup_path).close()
    parquet = tmp_path / "wikidata_taxa.parquet"
    _tiny_wikidata_frame().to_parquet(parquet, index=False)

    manifest_path = tmp_path / "candidates.manifest.json"
    kwargs = dict(
        candidates_path=tmp_path / "candidates.parquet",
        manifest_path=manifest_path,
        lookup_sqlite_path=lookup_path,
        wikidata_parquet_path=parquet,
        processes=1,
    )
    candidates.build_candidates_cache(_tiny_wikidata_frame(), **kwargs)
    before = json.loads(manifest_path.read_text())

    changed = _tiny_wikidata_frame()
    changed.loc[0, "name"] = "Acer rubrum"
    changed.to_parquet(parquet, index=False)

    candidates.build_candidates_cache(changed, **kwargs)
    assert json.loads(manifest_path.read_text()) != before


def test_source_fingerprints_always_carry_both_keys(tmp_path):
    """Omitting the key entirely made the dict comparison fail forever, so a manifest written by
    `python -m src.candidates` could never match a call that left the parquet path out."""
    a = candidates._source_fingerprints(tmp_path / "nope.sqlite", None)
    b = candidates._source_fingerprints(tmp_path / "nope.sqlite", tmp_path / "nope.parquet")
    assert set(a) == set(b) == {"lookup_sqlite", "wikidata_parquet"}


def test_file_fingerprint_tracks_content_not_metadata(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    first = file_fingerprint(path)

    os.utime(path, (1, 1))
    assert file_fingerprint(path) == first

    path.write_bytes(b"hellp")
    assert file_fingerprint(path) != first
    assert file_fingerprint(tmp_path / "missing") is None


# -- worker count must be controllable from outside the process --------------------------------


def test_worker_count_prefers_the_explicit_argument(monkeypatch):
    monkeypatch.setenv("MATCHER_WORKERS", "3")
    assert worker_count(7) == 7


def test_worker_count_honours_the_environment(monkeypatch):
    """os.cpu_count() and os.process_cpu_count() both report host cores under a --cpus quota, so
    this variable is the only thing that reliably caps the pool in a container."""
    monkeypatch.setenv("MATCHER_WORKERS", "2")
    assert worker_count() == 2


def test_worker_count_is_capped(monkeypatch):
    monkeypatch.delenv("MATCHER_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 256)
    monkeypatch.delattr(os, "process_cpu_count", raising=False)
    assert worker_count() == 16


# -- the data directory must actually be indirected --------------------------------------------


def test_data_dir_follows_the_environment(tmp_path, monkeypatch):
    """Constants are read at import time, so this reloads the module the way a fresh process
    would see it — which is what a container does."""
    import importlib

    monkeypatch.setenv("MATCHER_DATA_DIR", str(tmp_path / "elsewhere"))
    paths = importlib.reload(importlib.import_module("src.paths"))
    try:
        assert paths.DATA_DIR == tmp_path / "elsewhere"
        assert paths.MODEL_DIR == tmp_path / "elsewhere" / "models"
    finally:
        monkeypatch.delenv("MATCHER_DATA_DIR")
        importlib.reload(paths)


def test_model_dir_can_be_overridden_independently(tmp_path, monkeypatch):
    """A volume mounted over data/ hides the frozen models baked into the image."""
    import importlib

    monkeypatch.setenv("MATCHER_DATA_DIR", str(tmp_path / "vol"))
    monkeypatch.setenv("MATCHER_MODEL_DIR", str(tmp_path / "frozen"))
    paths = importlib.reload(importlib.import_module("src.paths"))
    try:
        assert paths.MODEL_DIR == tmp_path / "frozen"
    finally:
        monkeypatch.delenv("MATCHER_DATA_DIR")
        monkeypatch.delenv("MATCHER_MODEL_DIR")
        importlib.reload(paths)


def test_sqlite_index_built_by_the_fixture_is_readable(lookup):
    """Guards the conftest fixture itself, which every other candidate test leans on."""
    assert isinstance(lookup, sqlite3.Connection)
