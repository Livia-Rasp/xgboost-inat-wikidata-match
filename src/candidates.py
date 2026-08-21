"""Candidate generation from the SQLite taxa index. See docs/inat-wikidata-match-spec.md §2.

Covers milestone 1 (ingest: the normalised-name / trigram lookup tables) and milestone 3 (the
five candidate-generation strategies, unioned and capped to K per Wikidata item).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
from pathlib import Path

import pandas as pd
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

from .normalize import normalize_name

DEFAULT_TAXA_DB_PATH = Path.home() / ".cache" / "wikidata-inat-checker" / "taxa.db"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "lookup.sqlite"

# Every strategy tag generate_candidates() can attach to a candidate — the fixed source of truth
# for features.py's one-hot strategy_* columns. Must stay a *fixed* list, not derived from
# whatever tags happen to appear in a given candidate set: a small subset (e.g. a partial gold
# sample) can easily go a whole run without ever hitting synonym/basionym matches, and deriving
# columns from what's present would silently produce fewer columns than FEATURE_COLUMNS expects.
STRATEGY_TAGS = (
    "exact", "genus_epithet_fuzzy", "epithet_genus_fuzzy", "trigram",
    "synonym_exact", "synonym_genus_epithet_fuzzy", "synonym_epithet_genus_fuzzy",
    "basionym_exact", "basionym_genus_epithet_fuzzy", "basionym_epithet_genus_fuzzy",
)

# Ranks at or below species level. A row at one of these ranks whose name normalize_name()
# can't parse a specific_epithet out of is a provisional/unresolved iNat name (e.g. "Cortinarius
# sp. 'AZ19'") with no stable identity to match a Wikidata species against — excluded below.
_SPECIES_OR_BELOW_RANKS = {"species", "subspecies", "variety", "form", "hybrid"}

_EXPECTED_COLUMNS = {
    "taxon_id",
    "name",
    "rank",
    "ancestry",
    "normalized_name",
    "genus",
    "specific_epithet",
}


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _cache_is_valid(cache_path: Path, taxa_db_path: Path) -> bool:
    if not cache_path.exists():
        return False
    if cache_path.stat().st_mtime < taxa_db_path.stat().st_mtime:
        return False
    try:
        conn = sqlite3.connect(cache_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(taxa_normalized)")}
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    # A schema change (e.g. adding genus/specific_epithet) doesn't touch taxa.db's mtime, so the
    # mtime check alone can't catch it — mirrors the Node project's own dbIsStale(), which checks
    # for the presence of the `ancestry` column for exactly this reason.
    return _EXPECTED_COLUMNS.issubset(columns)


def build_lookup_cache(
    taxa_db_path: Path = DEFAULT_TAXA_DB_PATH,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> sqlite3.Connection:
    """Return a connection to the normalised-name + trigram lookup cache, rebuilding it from
    taxa_db_path if it's missing, older than the source db, or built under an older schema."""
    if _cache_is_valid(cache_path, taxa_db_path):
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
                normalized_name TEXT NOT NULL,
                genus TEXT,
                specific_epithet TEXT
            )
            """
        )
        cache.execute("CREATE INDEX idx_normalized_name ON taxa_normalized(normalized_name)")
        cache.execute("CREATE INDEX idx_genus ON taxa_normalized(genus)")
        cache.execute("CREATE INDEX idx_specific_epithet ON taxa_normalized(specific_epithet)")
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
        skipped = 0
        for taxon_id, name, rank, ancestry in rows:
            n = normalize_name(name)
            if rank in _SPECIES_OR_BELOW_RANKS and n.specific_epithet is None:
                skipped += 1
                continue
            batch.append((taxon_id, name, rank, ancestry, n.normalized, n.genus, n.specific_epithet))
            if len(batch) >= 10_000:
                _flush_batch(cache, batch)
                batch = []
        if batch:
            _flush_batch(cache, batch)

        cache.commit()
        print(f"skipped {skipped:,} provisional/unresolved names (no parseable epithet)")
    finally:
        source.close()

    return cache


def _flush_batch(cache: sqlite3.Connection, batch: list[tuple]) -> None:
    cache.executemany(
        "INSERT INTO taxa_normalized VALUES (?, ?, ?, ?, ?, ?, ?)",
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


# ---- Candidate generation (milestone 3, spec §2) ----------------------------------------

K = 20
MAX_EDIT_DISTANCE = 2
TRIGRAM_LIMIT = 10

DEFAULT_CANDIDATES_PATH = Path(__file__).resolve().parent.parent / "data" / "candidates.parquet"
DEFAULT_CANDIDATES_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "candidates.manifest.json"
)


def _exact_candidates(cache: sqlite3.Connection, normalized_name: str) -> list[dict]:
    """Strategy 1: every iNat taxon whose normalised name equals the query's."""
    if not normalized_name:
        return []
    rows = cache.execute(
        "SELECT taxon_id, name, rank, ancestry, normalized_name FROM taxa_normalized WHERE normalized_name = ?",
        (normalized_name,),
    ).fetchall()
    return [{"taxon_id": r[0], "name": r[1], "rank": r[2], "ancestry": r[3], "normalized_name": r[4]} for r in rows]


def _fuzzy_filter(rows: list[tuple], query: str, compare_col: int) -> list[dict]:
    """Keep rows whose value in compare_col is within MAX_EDIT_DISTANCE of query. Uses
    rapidfuzz's batch process.extract (one C-level pass over all choices) rather than a Python
    loop calling Levenshtein.distance per row — ~2x faster on the largest groups (e.g. the
    3,151-member 'astragalus' genus group), and the query stays a single expression either way."""
    rows = [r for r in rows if r[compare_col]]  # exclude NULLs (e.g. a genus-rank row has no epithet)
    if not rows:
        return []
    choices = [r[compare_col] for r in rows]
    matches = process.extract(query, choices, scorer=Levenshtein.distance, score_cutoff=MAX_EDIT_DISTANCE, limit=None)
    return [
        {"taxon_id": rows[idx][0], "name": rows[idx][1], "rank": rows[idx][2], "ancestry": rows[idx][3], "normalized_name": rows[idx][5]}
        for _, _, idx in matches
    ]


def _genus_fuzzy_candidates(cache: sqlite3.Connection, genus: str | None, epithet: str | None) -> list[dict]:
    """Strategy 2: same genus, epithet within MAX_EDIT_DISTANCE (orthographic variants, gender
    agreement)."""
    if not genus or not epithet:
        return []
    rows = cache.execute(
        "SELECT taxon_id, name, rank, ancestry, specific_epithet, normalized_name FROM taxa_normalized WHERE genus = ?",
        (genus,),
    ).fetchall()
    return _fuzzy_filter(rows, epithet, compare_col=4)


def _epithet_fuzzy_candidates(cache: sqlite3.Connection, epithet: str | None, genus: str | None) -> list[dict]:
    """Strategy 3: same epithet, genus within MAX_EDIT_DISTANCE (genus transfers, misspellings)."""
    if not genus or not epithet:
        return []
    rows = cache.execute(
        "SELECT taxon_id, name, rank, ancestry, genus, normalized_name FROM taxa_normalized WHERE specific_epithet = ?",
        (epithet,),
    ).fetchall()
    return _fuzzy_filter(rows, genus, compare_col=4)


_CHUNK_SIZE = 6
_CHUNK_STRIDE = 4


def _trigram_candidates(cache: sqlite3.Connection, normalized_name: str, limit: int = TRIGRAM_LIMIT) -> list[dict]:
    """Strategy 4: top-N by trigram similarity on the full normalised name.

    FTS5's default MATCH is an implicit AND of all query trigrams, which returns zero rows for
    anything but a near-exact hit — too strict for fuzzy matching. The fix is bm25-ranked OR, but
    OR-joining *every* individual 3-gram of a name (~25 for a typical binomial) is disastrously
    slow in practice: each trigram is individually common, so the OR matches a huge share of the
    1.4M-row corpus before bm25 can rank it — ~550ms/query, ~9 hours for the full pull. OR-joining
    a handful of overlapping 6-character chunks instead (still tokenized into trigrams internally
    by FTS5, just as a run of 4 rather than 1) is far more selective — same query drops to
    5-35ms — and ranks *better*, not worse: chunks are contiguous substrings, so a typo in one
    chunk still leaves neighbouring chunks intact (verified live: 'rossa canina' -> top hit
    'rosa canina'; 'prunela' -> both real Prunella rows in the top 2).
    """
    if not normalized_name:
        return []
    name = normalized_name.strip()
    if len(name) <= _CHUNK_SIZE:
        chunks = [name]
    else:
        chunks = [name[i : i + _CHUNK_SIZE] for i in range(0, len(name) - _CHUNK_SIZE + 1, _CHUNK_STRIDE)]
    if not chunks:
        return []
    match_expr = " OR ".join(f'"{c}"' for c in chunks)
    rows = cache.execute(
        """
        SELECT taxa_trigram.taxon_id, taxa_normalized.name, taxa_normalized.rank, taxa_normalized.ancestry, taxa_normalized.normalized_name
        FROM taxa_trigram JOIN taxa_normalized ON taxa_normalized.taxon_id = taxa_trigram.taxon_id
        WHERE taxa_trigram MATCH ?
        ORDER BY bm25(taxa_trigram) LIMIT ?
        """,
        (match_expr, limit),
    ).fetchall()
    return [{"taxon_id": r[0], "name": r[1], "rank": r[2], "ancestry": r[3], "normalized_name": r[4]} for r in rows]


def generate_candidates(
    cache: sqlite3.Connection,
    name: str,
    synonym_names: list[str] | None = None,
    basionym_names: list[str] | None = None,
    k: int = K,
) -> list[dict]:
    """Union strategies 1-5 for one Wikidata taxon name, cap at k. Every candidate is tagged
    with which strategy/strategies produced it. Strategy-1 exact matches on the primary name are
    always kept; the rest of the K slots go to the highest-similarity candidates from strategies
    2-5, ranked by a single normalised-similarity score computed uniformly (regardless of which
    strategy found them) so heterogeneous per-strategy scores don't need to be compared directly.
    """
    n = normalize_name(name)
    pool: dict[str, dict] = {}

    def add_all(cands: list[dict], tag: str, exact_primary: bool = False) -> None:
        for c in cands:
            entry = pool.setdefault(
                c["taxon_id"],
                {
                    "name": c["name"],
                    "rank": c["rank"],
                    "ancestry": c["ancestry"],
                    "normalized_name": c["normalized_name"],
                    "strategies": set(),
                    "exact_primary": False,
                },
            )
            entry["strategies"].add(tag)
            if exact_primary:
                entry["exact_primary"] = True

    add_all(_exact_candidates(cache, n.normalized), "exact", exact_primary=True)
    add_all(_genus_fuzzy_candidates(cache, n.genus, n.specific_epithet), "genus_epithet_fuzzy")
    add_all(_epithet_fuzzy_candidates(cache, n.specific_epithet, n.genus), "epithet_genus_fuzzy")
    add_all(_trigram_candidates(cache, n.normalized), "trigram")

    for syn in synonym_names or []:
        sn = normalize_name(syn)
        add_all(_exact_candidates(cache, sn.normalized), "synonym_exact")
        add_all(_genus_fuzzy_candidates(cache, sn.genus, sn.specific_epithet), "synonym_genus_epithet_fuzzy")
        add_all(_epithet_fuzzy_candidates(cache, sn.specific_epithet, sn.genus), "synonym_epithet_genus_fuzzy")
    for bas in basionym_names or []:
        bn = normalize_name(bas)
        add_all(_exact_candidates(cache, bn.normalized), "basionym_exact")
        add_all(_genus_fuzzy_candidates(cache, bn.genus, bn.specific_epithet), "basionym_genus_epithet_fuzzy")
        add_all(_epithet_fuzzy_candidates(cache, bn.specific_epithet, bn.genus), "basionym_epithet_genus_fuzzy")

    for entry in pool.values():
        # Reuse the candidate's already-normalized name from taxa_normalized instead of
        # re-running normalize_name() here — this loop runs for every candidate of every one of
        # 58k+ items, and re-parsing (NFKD decomposition, token classification) each time was a
        # measurable chunk of the total runtime for no benefit over the stored value.
        cand_normalized = entry["normalized_name"]
        entry["similarity"] = (
            Levenshtein.normalized_similarity(n.normalized, cand_normalized)
            if n.normalized and cand_normalized
            else 0.0
        )

    # Strict cap at k: exact matches go first (highest-precision signal, most likely to contain
    # the true positive), but even that set is truncated by similarity if it alone exceeds k, so
    # every item's candidate set is bounded uniformly for the group-size-based features
    # (n_candidates, sim_rank_in_group) regardless of how large a name collision happens to be.
    exact_primary = sorted(
        ((tid, e) for tid, e in pool.items() if e["exact_primary"]),
        key=lambda item: -item[1]["similarity"],
    )[:k]
    rest = sorted(
        ((tid, e) for tid, e in pool.items() if not e["exact_primary"]),
        key=lambda item: -item[1]["similarity"],
    )
    kept = exact_primary + rest[: max(0, k - len(exact_primary))]

    return [
        {
            "taxon_id": tid,
            "name": e["name"],
            "rank": e["rank"],
            "ancestry": e["ancestry"],
            "strategies": "|".join(sorted(e["strategies"])),
            "similarity": e["similarity"],
        }
        for tid, e in kept
    ]


def _candidates_manifest_matches(manifest_path: Path, source_mtimes: dict) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("k") == K
        and manifest.get("max_edit_distance") == MAX_EDIT_DISTANCE
        and manifest.get("source_mtimes") == source_mtimes
    )


_worker_cache: sqlite3.Connection | None = None
_worker_cache_path: Path | None = None


def _init_worker(cache_path: Path) -> None:
    global _worker_cache, _worker_cache_path
    _worker_cache_path = cache_path
    _worker_cache = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)


def _generate_for_row(row: tuple) -> list[dict]:
    qid, name, synonym_names, basionym_names = row
    cands = generate_candidates(
        _worker_cache,
        name,
        list(synonym_names) if synonym_names is not None else [],
        list(basionym_names) if basionym_names is not None else [],
    )
    return [
        {
            "wikidata_qid": qid,
            "inat_taxon_id": c["taxon_id"],
            "inat_name": c["name"],
            "inat_rank": c["rank"],
            "strategies": c["strategies"],
            "similarity": c["similarity"],
        }
        for c in cands
    ]


def build_candidates_cache(
    wikidata_taxa: pd.DataFrame,
    candidates_path: Path = DEFAULT_CANDIDATES_PATH,
    manifest_path: Path = DEFAULT_CANDIDATES_MANIFEST_PATH,
    lookup_sqlite_path: Path = DEFAULT_CACHE_PATH,
    wikidata_parquet_path: Path | None = None,
    force_refresh: bool = False,
    processes: int | None = None,
) -> pd.DataFrame:
    """Generate candidates for every row in wikidata_taxa, cached to parquet. Rebuilds whenever
    K, MAX_EDIT_DISTANCE, or either source file's mtime changes (both are themselves versioned
    caches, so this transitively picks up e.g. a re-pulled Wikidata parquet or a rebuilt
    lookup.sqlite).

    This is pure local SQLite/CPU work (no network calls), and each item's generation is
    independent read-only work against lookup.sqlite, so it's parallelized with a process pool —
    one read-only connection per worker (SQLite supports concurrent readers fine)."""
    source_mtimes = {"lookup_sqlite": lookup_sqlite_path.stat().st_mtime}
    if wikidata_parquet_path is not None and wikidata_parquet_path.exists():
        source_mtimes["wikidata_parquet"] = wikidata_parquet_path.stat().st_mtime

    if not force_refresh and candidates_path.exists() and _candidates_manifest_matches(manifest_path, source_mtimes):
        return pd.read_parquet(candidates_path)

    tasks = [
        (wd.qid, wd.name, wd.synonym_names, wd.basionym_names)
        for wd in wikidata_taxa.itertuples(index=False)
    ]
    n_workers = processes or min(os.cpu_count() or 4, 16)

    rows: list[dict] = []
    with mp.Pool(n_workers, initializer=_init_worker, initargs=(lookup_sqlite_path,)) as pool:
        for result in pool.imap_unordered(_generate_for_row, tasks, chunksize=200):
            rows.extend(result)

    df = pd.DataFrame(rows)
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(candidates_path, index=False)
    manifest_path.write_text(
        json.dumps(
            {"k": K, "max_edit_distance": MAX_EDIT_DISTANCE, "source_mtimes": source_mtimes},
            indent=2,
        )
    )
    return df


def recall_at_k(candidates: pd.DataFrame, wikidata_taxa: pd.DataFrame) -> float:
    """Fraction of Wikidata items whose true iNat taxon_id (its own inat_id, from P3151) appears
    somewhere in that item's generated candidate set. Spec §7 milestone 3's acceptance check."""
    true_by_qid = wikidata_taxa.set_index("qid")["inat_id"]
    found = candidates.groupby("wikidata_qid")["inat_taxon_id"].apply(set)
    hits = sum(
        1 for qid, true_id in true_by_qid.items() if qid in found.index and true_id in found[qid]
    )
    return hits / len(true_by_qid)


def recall_ceiling_report(cache: sqlite3.Connection, candidates: pd.DataFrame, wikidata_taxa: pd.DataFrame) -> dict:
    """Breaks the raw recall figure down by whether the true iNat taxon_id even exists in the
    active-taxa index at all. A meaningful fraction of P3151 links turn out to point to iNat
    taxon_ids that no longer exist as active taxa (deactivated/merged/stale references) — those
    can never be found by any candidate-generation strategy, since we only index active taxa,
    same as the source Node project. That's a P3151 data-quality issue (in the same family as
    milestone 2's reference-rate finding), not a candidate-generation gap, so it's reported
    separately rather than folded into one number that would understate how well generation
    itself is doing."""
    existing_ids = {r[0] for r in cache.execute("SELECT taxon_id FROM taxa_normalized")}
    true_by_qid = wikidata_taxa.set_index("qid")["inat_id"]
    stale = true_by_qid[~true_by_qid.isin(existing_ids)]
    resolvable = wikidata_taxa[~wikidata_taxa["qid"].isin(stale.index)]

    return {
        "raw_recall": recall_at_k(candidates, wikidata_taxa),
        "n_total": len(wikidata_taxa),
        "n_stale_p3151": len(stale),
        "stale_p3151_rate": len(stale) / len(wikidata_taxa),
        "resolvable_recall": recall_at_k(candidates, resolvable),
        "n_resolvable": len(resolvable),
    }


if __name__ == "__main__":
    import time

    from .wikidata import DEFAULT_CACHE_PATH as WIKIDATA_PARQUET_PATH
    from .wikidata import build_pull_cache

    conn = build_lookup_cache()
    matches = lookup_by_normalized_name(conn, "prunella")
    print(f"{len(matches)} match(es) for 'prunella':")
    for row in matches:
        print(f"  {row}")

    wd = build_pull_cache().taxa
    start = time.monotonic()
    candidates = build_candidates_cache(wd, wikidata_parquet_path=WIKIDATA_PARQUET_PATH)
    elapsed = time.monotonic() - start
    print(f"\n{len(candidates):,} candidate rows for {wd['qid'].nunique():,} Wikidata items ({elapsed:.1f}s)")

    report = recall_ceiling_report(conn, candidates, wd)
    print(f"raw recall @ K={K}: {report['raw_recall']:.2%} (spec target: >=97%)")
    print(
        f"  {report['n_stale_p3151']:,}/{report['n_total']:,} ({report['stale_p3151_rate']:.2%}) "
        "true P3151 links point to iNat taxon_ids that don't exist in the active-taxa index "
        "(stale/deactivated) -- unreachable by any strategy, not a generation gap"
    )
    print(
        f"  recall among the {report['n_resolvable']:,} resolvable items: "
        f"{report['resolvable_recall']:.2%}"
    )
