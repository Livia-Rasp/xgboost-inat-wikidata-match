# CLAUDE.md

Guidance for working in this repository.

A record-linkage classifier (XGBoost) that matches Wikidata taxon items to candidate
iNaturalist taxa, built on data from
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker).

Vault note: `XGBoost iNat Wikidata Match` in knowledge vault. Project-level ToDos
live there, not here — query with `vault_tasks` / `vault_overview` (`winged-eye-obsidian` MCP,
read-only; never write to the vault).

## The spec

**[`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md) is the design doc for
this whole project — read it before writing any code.** It fixes the repo layout, the candidate
generation strategy, the feature set, the model config, the evaluation metrics, and an ordered,
checkable milestone list (§7). Follow it rather than improvising an alternative shape; if a part
of it turns out to be wrong once code exists, that is a discussion to have with Livia, not a
silent deviation.

## Commands

Prerequisite: `~/.cache/wikidata-inat-checker/taxa.db` must exist — this repo reads it read-only
but never builds it. Built by running a checker (e.g. `npm run links`) in the sibling
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker) repo; see README.md
for the full command.

Needs the venv for all of the below (`pandas`/`pyarrow`/`requests`/`rapidfuzz`/`matplotlib`):
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` once.

- **Ingest check (milestone 1)** — builds/reuses the normalised-name + FTS5 trigram lookup
  cache at `data/lookup.sqlite` from `~/.cache/wikidata-inat-checker/taxa.db` (excluding ~4.5k
  provisional/unresolved iNat names with no parseable epithet — see milestone 3), then looks up
  `prunella` (spec's acceptance check — a genus name shared by a bird family and a mint family).
  Run from the repo root.
  ```
  .venv/bin/python -m src.candidates
  ```
  First run ~20s over 1.4M rows; reruns are a cache hit unless `taxa.db`'s mtime changes or the
  schema version bumps (checked via `PRAGMA table_info`, not just mtime — mirrors the Node
  project's own `dbIsStale()` check, for the same reason: a schema change doesn't touch
  `taxa.db`'s mtime).

- **Wikidata pull (milestone 2)** — batched SPARQL against `query.wikidata.org`, `LIMIT`-capped
  to 60,000 taxa with P3151 (of 856,040 that actually have it), plus each item's P1420 synonym
  and P566 basionym names, cached to `data/wikidata_taxa.parquet` + a manifest the cache-hit
  check compares request shape against.
  ```
  .venv/bin/python -m src.wikidata
  ```
  First run: a few minutes, ~30 batched POST requests (2000 QIDs/batch, timed against the real
  endpoint). Reruns are a cache hit unless the target size or column set changes — no
  time-based staleness check, since there's no local source file to diff against and this
  shouldn't silently re-hit a shared public endpoint. Pass `force_refresh=True` to
  `build_pull_cache()` for a deliberate re-pull.

- **Candidate generation (milestone 3)** — the five strategies from spec §2 (exact match,
  genus-fixed/epithet-fuzzy, epithet-fixed/genus-fuzzy, trigram top-10 via overlapping
  6-character chunks — not per-trigram OR, which is ~100x slower for no ranking benefit —
  and synonym/basionym), capped at K=20 per item, cached to `data/candidates.parquet` +
  manifest. Pure local SQLite/CPU work, no network — parallelized across `os.cpu_count()` worker
  processes, capped at 16 (`multiprocessing.Pool`, one read-only connection per worker; note
  `os.cpu_count()`/`nproc` report logical CPUs, which may exceed physical cores under
  virtualization).
  ```
  .venv/bin/python -m src.candidates
  ```
  ~1-2 minutes for the full pull here (was ~15 minutes single-threaded before parallelizing, and
  would be ~9 hours with the naive per-trigram-OR approach). Reruns are a
  cache hit unless K, the edit-distance threshold, or either source cache's mtime changes.
  Reports the recall-ceiling breakdown (raw vs. resolvable-only, since ~12.85% of P3151 links
  point to iNat taxon_ids that no longer exist as active taxa — a data-quality issue, not a
  generation gap).

This section gets filled in further as the remaining milestones (§7) land, with the exact
runnable commands and their flags.

## Conventions

Python, per the spec's §0 repo shape (`src/`, `pyproject.toml`, `data/` gitignored,
`gold/hard_cases.csv` committed).

**Documentation stays current.** Update `README.md` and this file's Commands section in the same
commit as the code change they describe, not as a follow-up. A milestone isn't done until its
runnable command is documented here and reproducible from a clean checkout — that's also what
makes each milestone's "Check:" line in the spec verifiable by someone other than whoever wrote
the code.
