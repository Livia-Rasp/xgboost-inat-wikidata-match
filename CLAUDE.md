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

- **Features + splits (milestone 4)** — `src/labels.py` (P3151 positives/negatives, spec §3's
  15% synthetic abstention dropout, `no_answer_reason` tagging `stale_p3151` separately from
  `synthetic_dropout`) and `src/features.py` (every feature group in spec §4, `GroupKFold`
  splits). Needs `src/wikidata.py`'s `build_ancestor_chains()` first — one hop of P171 (milestone
  2) isn't enough for `kingdom_match`/`family_match`/`order_match`/`shared_ancestor_depth`, so
  this pulls the full transitive chain (`wdt:P171+`, mirroring the Node project's
  `fetchWdAncestorChains`), cached separately to `data/wikidata_ancestors.parquet`. **Caution:**
  WDQS can return an HTTP 200 with a silently incomplete result for this query under load (seen
  live: one batch came back 140/750, a retry got 750/750) — `build_ancestor_chains()` guards
  against this with a coverage-ratio check and retry, not just the usual HTTP-status retry.
  `WD_RANK_TO_NAME` (Wikidata rank QID → rank name) in `labels.py` was derived empirically by
  cross-tabulating known WD/iNat rank pairs from the resolvable population, not hardcoded from
  memory; `RANK_LEVEL` reuses wikidata-inat-checker's own `RANK_ORDER` constant
  (`lib/getInatTaxaDb.js`) rather than inventing a new numbering.
  ```
  .venv/bin/python -m src.features
  ```
  First run: ~8 min (ancestor pull, network, one-time) + <1 min (everything else, local).
  Reruns are a cache hit unless the upstream caches' row counts change or `N_SPLITS` changes.

- **Baseline (milestone 5)** — `src/evaluate.py`: the honest exact-match rule (spec §6), tie-
  broken by real iNat observation count. Spec's own wording implies sourcing that offline from
  `observations.csv.gz`, but that file is 12.7 GB (vs. `taxa.csv.gz`'s 39.5 MB) — disproportionate
  for a signal only ever consulted on the ~27.5k taxa actually involved in an exact-match tie
  (6,842 of 58,842 items have one). Sourced from the iNat API instead
  (`GET /v1/taxa?id=a,b,c,...`, batches 200/request, ~138 requests total), scoped to just those
  tied taxa — cached to `data/inat_observation_counts.parquet` + manifest (same deterministic-
  fingerprint approach as the ancestor-chain cache, reusing `wikidata.py`'s `_qid_set_fingerprint`
  and `RateLimiter`). An old (2022) forum post describes `observations_count` capping at 10,000
  on this endpoint; a live check found no such cap in practice (got 222,630 for one taxon).
  ```
  .venv/bin/python -m src.evaluate
  ```
  First run: ~2-3 min (network, one-time, scoped to the tied subset only). Reruns are a cache
  hit. Scores per fold and overall (spec's "same folds" requirement) plus abstention accuracy
  split by `no_answer_reason` — the naive rule can't distinguish "genuinely no candidate exists"
  (`stale_p3151`, 85.3% correctly abstained) from "the true candidate's label was hidden for this
  exercise but the candidate itself is still right there" (`synthetic_dropout`, only 2.1%) — a
  limitation the trained model (milestone 6) should improve on.

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
