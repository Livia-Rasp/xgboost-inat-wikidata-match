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
  Reruns are a cache hit unless `N_SPLITS` changes, or any of the four inputs changes — keyed on
  `paths.file_fingerprint()` content hashes (`_features_source_fingerprints()`,
  `FEATURE_SOURCE_NAMES`), not on the row counts it used to compare. Row counts cannot see a
  change in feature *values*: rebuilding the same 590,671 rows with different numbers in them
  leaves every count identical, so the cache reported a hit and handed back the previous frame.
  Every key is always present, `None` for a source whose path was not passed — milestone 13's
  lesson (a sometimes-absent key can never match a dict comparison), applied to the second
  manifest. `source_paths=` is how a caller opts in; `python -m src.features` passes all four.

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

- **Model + threshold selection (milestone 6)** — `src/train.py`: two objective variants on the
  same 5-fold OOF CV (`build_oof_predictions()`, cached to `data/oof_predictions.parquet` +
  manifest) — `binary:logistic` with per-fold `scale_pos_weight` (12.5:1 imbalance), and
  `rank:map` (not spec's literal `rank:pairwise` — XGBoost's current docs recommend `rank:map`
  specifically for binary-relevance labels with enough data, which is exactly this problem).
  Both get `monotone_constraints` built programmatically from `FEATURE_COLUMNS`
  (`monotone_constraints_tuple()`) rather than a hardcoded position tuple — verified live: zero
  monotonicity violations sweeping `jaro_winkler_full`/`kingdom_match`/`shared_ancestor_depth`.
  `XGBRanker.fit()` needs a numeric `qid`, not the raw `wikidata_qid` string — factorized via
  `pd.factorize()`.
  `TREE_PARAMS` includes a fixed `random_state` — `subsample`/`colsample_bytree` make training
  nondeterministic without one, which would silently break milestone 6's own literal check
  ("precision-at-threshold table reproduces"). The OOF cache's manifest (`shape_key`) includes
  `TREE_PARAMS` and `MONOTONE_UP` themselves, not just data shape — a hyperparameter change must
  invalidate the cache, the same way a schema change must for milestone 1's cache (§ above).
  `_oof_manifest_matches()` does a **subset** match (`shape_key`'s keys ⊆ manifest's keys), not
  exact dict equality — the manifest gains `*_avg_best_iteration` keys after training that
  `shape_key` never has going in, so exact equality would never match and every "cache hit" would
  silently retrain. (Both of these were real bugs caught here during development — worth keeping
  the guardrails, not just the fix, since the failure mode is silent either way: wrong-but-not-
  crashing results, not an exception.)
  `shape_key` is built by `oof_shape_key(features, features_path)` and carries a
  `features_fingerprint` — a third instance of the same silent failure. `n_rows`,
  `feature_columns`, `n_folds`, `tree_params` and `monotone_up` are all identical between two
  feature tables of the same shape holding different values, which is precisely what milestone
  15's retrain produces, so without the fingerprint the previous model's predictions come back as
  if they were the new ones. `None` when no path is passed, so the subset match still hits
  against a manifest written before the key existed — an absent path is not evidence of change,
  the rule `_cache_is_valid()` learned in milestone 13. Note a parquet byte-fingerprint also
  moves on a column reorder or a compression change, neither of which affects the model: that is
  a wasted rebuild, never a false hit, which is the right direction to fail in.
  ```
  .venv/bin/python -m src.train
  ```
  ~2 min for the full 590k-row × 5-fold × 2-objective run, pure local CPU; genuine cache hit on
  rerun is ~1.5s. `build_final_models(features)` (not run from `__main__` — call directly) refits
  both variants on all folds for milestone 7, using each variant's OOF-fold `best_iteration`s
  averaged as a fixed `n_estimators` (no held-out set exists once trained on everything), saved
  to `data/models/` with their calibrators pickled alongside.

  **Real finding, not a bug**: isotonic calibration on the OOF scores shows the raw model is
  badly overconfident (50,296 rows score ≥0.95 raw; only 83.9% are correct) — the strict
  99.5%-precision auto-accept band this produces covers only 10 rows for `binary:logistic` (none
  for `rank:map`). Investigated rather than just reported: inside that cluster, correct and
  incorrect rows are statistically indistinguishable across every engineered feature, and it's
  almost never a multi-candidate tie — most consistent with label noise in P3151 (spec §3's
  disclosed concern) capping what any feature set could achieve, not a deficiency in this model.
  Full investigation in the notebook.

- **Gold set (milestone 7, in progress)** — `build_gold_labeling_kit.py` and `build_gold_set.py`
  at the repo root (not `src/` — one-off tooling that drives the pipeline for a specific task,
  matching the convention spec's own milestone 12 implies for `score_ambiguous.py`), plus a
  `--gold` path added to `src/evaluate.py`'s `__main__`. Full workflow in `gold/README.md`.

  Gold-set items are Wikidata taxa **without** P3151 by construction (that's the checker's whole
  purpose, and spec's whole reason for using it — the only evaluation not contaminated by
  bot-added labels) — a disjoint population from every other cache in this project, all of which
  are keyed on items that *already have* P3151. `data/wikidata_taxa.parquet` and everything built
  from it are useless here. `wikidata.py` gained `fetch_attributes_batch_no_p3151()` +
  `build_gold_attribute_pull()` for this — the regular attribute pull's `?item p:P3151 ...`
  triple is mandatory and returns zero rows for these items, so this is a genuinely separate
  query, not a parameter flip, dropping the `inatId`/`p3151_has_reference` fields that don't
  apply and making everything else `OPTIONAL` as before. Cached to
  `data/gold_wikidata_attributes.parquet` (fingerprinted on the QID set, same pattern as the
  ancestor-chain cache). `build_ancestor_chains()` needed no changes — it never depended on
  P3151 in the first place.

  `gold/hard_cases.csv` is deliberately *not* the minimal schema the milestone-0 scaffold
  shipped with (`wikidata_qid,wikidata_name,inat_taxon_id,inat_name,label,notes`) — nothing
  there was ever set in stone. It now carries every column `features.build_features()` needs
  from a candidates.parquet-shaped frame (`inat_rank`, `strategies`, `similarity`) plus
  `found_by_generation` (did our own `candidates.py` actually surface the item's stated correct
  match, or did `build_gold_set.py` have to add it as an explicit extra row — a live
  recall-ceiling check on data candidate generation never saw during development, milestone 3's
  number cross-validated independently) — so the committed file is fully self-sufficient for
  `evaluate.py --gold` to score, no candidate regeneration needed at evaluation time.

  `evaluate.py --gold`'s headline analysis directly tests milestone 6's label-noise hypothesis:
  same raw-score band (`binary_raw_score >= 0.95`) that sat at 83.9% precision on OOF/P3151 data
  (`gold_band_comparison()`), now measured on hand-verified labels P3151 never touched. Also
  re-applies milestone 6's *exact* OOF-selected auto-accept threshold rather than sweeping a
  fresh one (`gold_threshold_check()`) — sweeping on ~300-500 gold rows would be circular/noisy,
  the point is whether a threshold chosen *without* seeing gold data still holds up.

  **Preliminary results are now in `README.md`'s Status section and the report notebook's own
  milestone 7 section**, both explicitly marked preliminary (192/476 labelled, A-C only). Adding
  the notebook section surfaced a real risk worth remembering: a full `jupyter nbconvert
  --execute` re-runs *every* cell, including milestones 1-6's, and milestone 1 reads
  `~/.cache/wikidata-inat-checker/taxa.db` live — which had refreshed since the notebook was
  first populated, silently changing milestone 1's `cortinarius` example (the taxon rows behind
  it had changed) and breaking that section's own written narrative. Caught before committing by
  diffing against the last commit's cell outputs; fixed by restoring cells 0-64 from git and
  keeping only the new milestone 7 cells from the fresh run. **Takeaway: never blanket
  re-execute this notebook** — earlier milestones' numbers are meant to be frozen at whatever
  `data/*.parquet` state they were built against, not re-derived from live external state on
  every append. Add new cells, execute only those (or the new range), and diff before saving.

  **Rank-trivial stratification.** While hand-labeling, Livia noticed a lot of "ambiguous" WD
  items are only a name collision, not a genuine judgment call — a species complex or section
  sharing its name string with its own representative species, where the WD item's stated rank
  (P105) matches exactly one candidate's `inat_rank` and not the other. `load_gold_features()`
  now flags these (`rank_trivial` column: exactly one candidate in the group has `rank_equal ==
  True`, group size ≥2) and `gold_rank_trivial_breakdown()` reports top-1 accuracy/MRR for that
  bucket separately from the genuine remainder. Trivial-bucket accuracy is a sanity floor, not a
  headline number — near-100% there is expected and not itself evidence of model quality; the
  informative number is how much accuracy drops on the non-trivial remainder, which is where the
  model's actual judgment gets tested. The report notebook's milestone 7 section carries this as
  its own subset breakdown, not folded into the pooled top-1/MRR numbers.
  ```sh
  cd ~/repos/wikidata-inat-checker && rm -f cache/cache-links.json && npm run links -- --limit 80000 --ambiguous-only
  cd - && .venv/bin/python build_gold_labeling_kit.py   # appends new items onto gold/labeling_filled.csv
  # (hand-label the new rows directly in gold/labeling_filled.csv — see gold/README.md; milestone 8
  # replaced the original separate labeling_template.csv "save as" step with this in-place append)
  .venv/bin/python build_gold_set.py                # writes gold/hard_cases.csv
  .venv/bin/python -m src.evaluate --gold           # scores it
  ```
  The `npm run links` step turned out to be genuinely fragile against real-world WDQS load, and
  took four attempts and two upstream fixes (both in the sibling repo, also owned by Livia Rasp)
  before it ran clean — worth recording in full since the failure modes recurred across separate
  runs and weren't obvious from a single crash:

  1. A live `--limit 150000` run took 66 minutes, got all the way through the main scan and
     P3151 cross-check, then crashed with a JSON parse `SyntaxError` inside the checker's own
     ancestor-chain fetch (`fetchWdAncestorChains` in `lib/utils.js`) — no mid-run checkpoint
     (`cache/cache-links.json` only skips *already-collected* QIDs on a *future* run). Retried
     smaller, at `--limit 80000` (still comfortably clears 300+ ambiguous rows at the observed
     rate: 863/150,000 = 0.575%, matching the checker's own documented ~0.57% baseline).
  2. That retry failed faster and differently: `Fatal error: [DOMException [TimeoutError]]`
     3m45s in. Root cause: the checker's `fetchWithRetry()` only retried on the HTTP status codes
     in `RETRYABLE_STATUS` (429/502/503/504) — a *rejected* `fetch()`, which is exactly what
     `AbortSignal.timeout(90_000)` produces on a hung connection, was never caught anywhere in
     that function and propagated straight up uncaught. **Fixed upstream**: `fetchWithRetry()`
     now catches `TimeoutError`/`AbortError` rejections and retries them with the same backoff as
     a 502.
  3. With that fix, the next run got much further (main scan + P3151 cross-check both completed,
     476 ambiguous items found) but crashed again in the *same* `fetchWdAncestorChains` call as
     attempt 1, this time `SyntaxError: Unterminated string in JSON` — a different offset, so not
     a fixed truncation point. Root cause: `sparql()` (`lib/utils.js`) had no protection at all
     against WDQS returning an HTTP 200 with a body truncated mid-stream under load — valid HTTP,
     invalid JSON, and `JSON.parse` was called with no `try`/`catch` around it. This is the exact
     same "silently incomplete WDQS response" failure mode this project's own
     `build_ancestor_chains()` already had to guard against (`ANCESTOR_MIN_COVERAGE`, milestone
     4's errors list above) — just surfacing as a hard parse crash here instead of a row
     undercount. **Fixed upstream**: `sparql()` now retries on a JSON parse failure with the same
     backoff as a bad status, instead of throwing immediately.
  4. Retried again with both fixes in place — they worked (log shows a caught timeout and three
     escalating parse-failure retries recovering) — but then hit *four* consecutive truncated
     responses for one single ancestor-chain batch and exhausted the default retry budget (3).
     49m25s in, ~2.5 hours cumulative across all four attempts.
  5. Rather than raise the retry budget and keep re-fighting WDQS on the same expensive code path
     indefinitely, stepped back and noticed the actual problem: `fetchWdAncestorChains` was being
     called on **all ~78,600 P3151-matched (non-ambiguous) items** — that data only feeds
     `output/links.html`'s auto-approve tree comparison, which this project never uses at all.
     The much smaller ambiguous-only ancestor fetch (~475 items) that `output/links-ambiguous.html`
     actually needs is a separate call a few dozen lines later in `checkLinks.js`, and was never
     itself the problem. **Added upstream**: a `checkLinks.js --ambiguous-only` flag that skips
     the P3151 cross-check, the large ancestor-chain fetch, and `links.html` generation entirely,
     going straight to the small ambiguous-only fetch + `links-ambiguous.html`. Documented in that
     repo's `docs/links.md`.

  With `--ambiguous-only`, `--limit 80000` completed in 5m36s — down from 45-90+ minutes and four
  failed attempts — and found 476 ambiguous items (well past the 300+ target). Sibling repo's own
  test suite (213 tests) passed after every change above.

  **The milestone 6/7 models are frozen as the report's fixed reference point.**
  `train.build_final_models()` already only (re)trains a variant when its `data/models/*.json`
  is missing or `force_refresh=True` is passed — otherwise it loads the existing file — so as
  long as `data/models/` isn't deleted and nothing calls it with `force_refresh=True`, the exact
  binaries behind every milestone 7 gold-set number stay fixed regardless of what else changes
  upstream. That matters concretely here: labeling the gold set has you submitting confirmed
  matches to Wikidata by hand (see below and `gold/README.md`), which is itself new P3151 data —
  a future from-scratch `wikidata.py` re-pull would see a different population than milestone
  2-6 trained on. Freezing the model means that drift can't silently change the report.
  `data/` stays gitignored as before (spec §0) — this is a documented policy, not a new
  committed-artifact convention; reproducing the frozen numbers from scratch means re-running the
  exact command sequence in this file in order, not deleting and regenerating `data/models/`.

  Found and fixed one real bug while making this freeze meaningful: `build_final_models()`'s
  `force_refresh`/exists check only gated whether the *model* was retrained — the calibrator was
  unconditionally recomputed and overwritten on every call, `fit()` against whatever `oof` was
  passed that call. A stale, frozen model could silently end up paired with a calibrator fit
  against different (e.g. re-pulled) OOF data — the exact drift this freeze is meant to prevent,
  just one file later. Fixed: the calibrator now only (re)fits in the same branch as the model,
  and is loaded from `data/models/*_calibrator.pkl` alongside the model otherwise.

- **Balance the gold sample across the alphabet (milestone 8, done)** — the 476-item ambiguous
  sample generated in milestone 7 covered only names from "Abietinella" to "Cattleya" (296/92/88
  split across A/B/C, nothing D-Z). Root cause: `allNames()` (`lib/getInatTaxaDb.js` in
  `wikidata-inat-checker`) runs `SELECT DISTINCT name FROM taxa` with no `ORDER BY`, but SQLite's
  `DISTINCT` implementation happens to produce alphabetically-sorted output as a side effect;
  `checkLinks.js` scanned names in that incidental order, so `--limit 80000` capped collected
  candidates before the scan ever reached past the C's — a systematic artifact, not sampling
  noise. Fixed with the smallest of three options considered (raising `--limit` further would
  reintroduce the WDQS load `--ambiguous-only` was built to reduce; bucketed per-letter scans
  would multiply that same fragile pipeline): **shuffle the name list before the `--limit`
  cutoff**, in the sibling repo. `lib/utils.js` gained `shuffle(arr, seed)` (seeded Fisher-Yates,
  mulberry32 PRNG — no RNG dependency existed in that zero-runtime-dependency repo, so hand-
  rolled rather than adding a package); `checkLinks.js` applies it to `taxaDb.allNames()` before
  the by-name path's collection loop (the `--iucn` path is already selective/small and untouched).
  Default seed `42` (matching `build_gold_labeling_kit.py`'s own `RANDOM_SEED`), overridable with
  `--seed <n>`. Scoped to that one call site, not inside `allNames()` itself, since
  `checkLinksStats.js` also calls it and has no reason to want randomized output. Three new tests
  in `test/utils.test.js` (determinism, permutation, seed-sensitivity); full suite (216 tests)
  passes. Documented in that repo's `docs/links.md`.

  Re-running `--ambiguous-only` with the fix found 491 ambiguous items spanning the full alphabet
  (A: 46, B: 19, C: 63, D: 24, ... X: 2, Z: 2 — was 100% A-C before). This repo's
  `build_gold_labeling_kit.py` became merge-aware to consume that safely: it reads
  `gold/labeling_filled.csv` first and excludes every QID already there (answered or deliberately
  left blank) from resampling. Originally capped the new-items pool at `500 - len(existing)` (24,
  that run) to keep the total near the milestone-7 target, but that meant only 24 of the 407
  genuinely-new items this scan actually found ever got surfaced — most of the alphabetic
  diversity the fix produced would have gone unsampled. Dropped the cap entirely on Livia's call:
  every new item is now included every run (`SAMPLE_SIZE`/`RANDOM_SEED` removed — nothing left to
  seed once there's no subsampling); leaving CSV rows blank is the intended way to bound labeling
  effort, not a smaller kit.

  Went a step further on Livia's own observation: the original design wrote a separate
  `labeling_template.csv` (existing rows carried forward + new blank ones appended) that she'd
  edit and "save as" `labeling_filled.csv` — but once the script started carrying existing answers
  forward byte-for-byte, that's *pure duplication*, not a safety boundary; the two files were
  identical for every already-answered row. Collapsed to one: `append_new_rows()` now appends new
  rows directly onto `gold/labeling_filled.csv` (creating it with a header on the very first run),
  never reading or rewriting a row that's already there — exactly as safe as the old carry-forward
  copy, minus the duplicate file and the manual "save as" step. `labeling_template.csv` is gone
  (removed from `.gitignore` too; its 407 already-appended rows were folded into
  `labeling_filled.csv` directly, verified identical before deleting). The sample HTML is
  unaffected — still scoped to just the new items each run, and still never overwrites a prior
  batch via `next_sample_html_path()` (`links-ambiguous-sample.html`, then `-2.html`, `-3.html`,
  etc. — the `.gitignore` pattern is now the glob `gold/links-ambiguous-sample*.html` to cover all
  of them).

  Found and fixed one real bug while running this for real: `write_trimmed_html()`'s original
  single-pass loop called `tr.decompose()` on unwanted rows *during* forward iteration over a
  precomputed `find_all("tr")` list. bs4's `decompose()` clears the whole `next_element` chain
  from the decomposed tag onward, and this HTML's table markup nests candidate rows deeply enough
  that the chain reached past the intended subtree into later, not-yet-inspected siblings —
  corrupting them (`tr.attrs` became `None`) before the loop ever read their `id`/`class`,
  crashing with `AttributeError: 'NoneType' object has no attribute 'get'` on the live 491-item
  HTML (not on the smaller/differently-shaped 476-item one, which is why milestone 7 never hit
  it). Fixed by splitting into two passes: decide what to remove first (reading every row's
  `id`/`class`/`data-qid` while the tree is still fully intact), then decompose only those rows,
  in *reverse* document order — later removals can no longer corrupt earlier rows this loop still
  needs to read.

  New batch (407 items, C through Z) lives at `gold/links-ambiguous-sample-2.html` + the appended
  tail of `gold/labeling_filled.csv` (rows 477-883), ready for hand-labeling alongside the
  original 476 — Livia doesn't need to answer all 407, just as many as she chooses.
  `gold/README.md`'s "Known limitation" note and its §2/§3 workflow description replaced with
  this fix and the new merge-aware, no-target-size, single-file workflow.
  ```sh
  cd ~/repos/wikidata-inat-checker && rm -f cache/cache-links.json && npm run links -- --limit 80000 --ambiguous-only
  cd - && .venv/bin/python build_gold_labeling_kit.py   # merge-aware: appends new rows straight onto gold/labeling_filled.csv
  ```

- **Discuss and finetune the results (milestone 9, done at n=263)** — spec gained
  this milestone this session, formalizing a practice that started informally: after every
  partial `--gold` run, review every top-1 miss individually against its full feature/score
  breakdown rather than trusting the aggregate accuracy/MRR, especially at small sample sizes
  where one row can swing the percentage. On the first 50-item test run this caught a real
  labeling error (`Q21438872` — a WD item whose stated rank matched a subgenus, but the true
  answer was the nominotypical genus of the same name; Livia corrected it directly), and
  surfaced — but did **not** yet act on — two real findings worth revisiting once more gold
  labels exist:
  1. `sim_rank_in_group`/`family_match` are currently unconstrained in `MONOTONE_UP`, and a case
     was found where the unconstrained interaction let a candidate with strictly worse taxonomic
     agreement (`kingdom_match`/`family_match`/`shared_ancestor_depth` all worse) outscore one
     with better agreement, for `rank:map` specifically (`binary:logistic` got the same case
     right). Extending monotone constraints to cover this was tested and does fix that raw-score
     ordering — but reverted rather than adopted, since a real before/after call needs more than
     one gold example to judge fairly.
  2. `top1_accuracy_and_mrr()`/`gold_top1_and_mrr()` rank candidates within a group by
     *calibrated* probability, but isotonic calibration is a step function that can map a wide
     range of distinct raw scores to the same output ("plateaus") — discarding real relative-
     ordering information. Negligible on the full 590k-row OOF population (~0.02pp difference
     between raw- and calibrated-score top1/MRR) but large on the small gold set: in one test,
     calibrated-based `rank:map` top-1 accuracy read 43.5% while the *same* model's raw-score
     top-1 accuracy was 95.65%, identical to `binary`. This bug already affects gold-set
     reporting today, independent of finding 1 — calibrated probability is still correct for
     anything needing cross-group comparability (auto-accept/reject thresholds, Brier score),
     just not for within-group ranking metrics.

  **Finding 2 is now implemented; finding 1 is still parked.** Ranking metrics moved to the raw
  score (`evaluate.ranking_score_column()`, with the reasoning in its docstring); calibrated
  probability stays in use for Brier and for both thresholds, where cross-group comparability is
  the whole point. Confirmed at n=263: `rank:map` reads 86.5% top-1 ranked by calibrated
  probability and **97.8%** ranked by its own raw scores, identical models and predictions. At OOF
  scale the same change is worth 0.03pp (99.15% → 99.12%), which is why it hid for so long.
  `src/train.py`'s `__main__` now prints both so the difference stays auditable. Finding 1
  (extending `MONOTONE_UP`) stays parked deliberately: adopting it means retraining, which breaks
  the model freeze every published number is quoted against — moved to `docs/future-work.md` as a
  fully-rescored comparison rather than a patch.

  **Final numbers at n=263** (263/883 sampled items answered, A-Z after milestone 8):
  binary 98.70%/0.9935 top-1/MRR, rank 97.83%/0.9891, baseline 20.91%; non-trivial-by-rank subset
  (206 items) binary 98.87% vs rank 97.74%; Brier 0.0130 vs 0.0243; band precision 98.2% on gold
  vs 83.9% on OOF (n=167); recall ceiling **100%**. **`binary:logistic` is the pick** — leads on
  every metric, widens on the non-trivial subset, and is the only variant clearing the 99.5%
  auto-accept bar at all. Rationale written up in `docs/findings.md` §6.

  The per-miss review paid off again: `Q4694188` (*Agrisius japonicus*) was labeled `265680`
  (*Poecilopompilus algidus*, a spider wasp) — a dropped leading digit from `1265680`, and not
  among the candidates the kit offered. It was also the *sole* reason the recall ceiling read
  99.57% rather than 100%, i.e. the "one genuine candidate-generation miss" was never a
  generation miss at all. Corrected in `gold/labeling_filled.csv` with a note in its `notes`
  column. The three surviving `binary` misses: one model gap (`Q121887868`, WD phylum vs
  identically-named iNat genus, where only the true candidate has `rank_equal`) and two iNat-side
  duplicate records (`Q16760098`, `Q46674974`) where no taxonomic feature can separate the pair.
  A new honest negative also came out of this run and is in `docs/findings.md` §2: the
  OOF-derived reject threshold does **not** transfer to the ambiguous population — 99.61% row
  precision on OOF, 95.7% on gold, and it would hide the true match for 107 of 263 items. That is
  why the README's headline is ranking quality rather than queue clearance.

  Earlier state, kept because the reasoning matters: recall
  ceiling corrected to 99.41% (one apparent candidate-generation miss, `Q4694188`) after fixing a
  second bug this run: `score_gold_set()`'s recall-ceiling calc took an arbitrary first row per
  item via `drop_duplicates`, which happened to mask that exact miss (reported 100%). Fixed by
  filtering to `label==1` rows before averaging `found_by_generation`. Also fixed
  `build_gold_set.py`'s `load_answers()`: default `pd.read_csv` NA parsing treats the literal
  string `"None"` as null, silently blanking and dropping Livia's `Q111270149` answer (she wrote
  "None," not the documented "NONE" — same intent, but the loader used `keep_default_na`'s
  default and lost the row entirely rather than reading it as a no-match answer). Fixed with
  `keep_default_na=False`.

- **QuickStatements export (milestone 10, not started)** — spec gained this milestone earlier
  this session: labeling the gold set already resolves ambiguous taxa an automated match
  couldn't, so those resolved links should go back into Wikidata as a batch, not just via the
  labeling HTML's per-row copy button (easy to miss rows, no consolidated record of what was
  submitted). Will read `gold/hard_cases.csv`'s confirmed matches (`label == 1`) and write one
  `{qid}\tP3151 "{inatId}"` line each to a `.qs` file for a single QuickStatements paste.
  Now tracked in `docs/future-work.md` rather than here, since the study itself is closed.

- **README figures (milestone 11)** — `build_figures.py` at the repo root writes six PNGs to
  `docs/img/` (three figures × light/dark, embedded through `<picture>` so GitHub serves the
  reader's theme). Reads `data/oof_predictions.parquet`, `data/features.parquet` and
  `data/models/binary_model.json`; no network.
  ```
  .venv/bin/python build_figures.py
  ```
  Deterministic on purpose — reruns are byte-identical, so a stale figure shows up as a diff
  instead of hiding. That needed two things beyond a fixed sample seed: `metadata={"Software":
  None}` on `savefig` (matplotlib otherwise stamps its own version into the PNG), and seeding
  numpy's **global** RNG immediately before `shap.plots.beeswarm`, which shuffles tied points
  through it. Colours come from the `dataviz` skill's validated palette and were re-validated
  all-pairs in both modes.

- **Docker + lockfile (milestone 13, done)** — the pipeline runs with no venv, no Node and
  nothing on the host. `docker/Dockerfile` (pipeline) and `docker/Dockerfile.airflow` (built but
  unused until milestone 16), `compose.yaml`, `Makefile`, `.dockerignore`, `uv.lock`.
  ```sh
  docker compose run --rm pipeline make gold        # the acceptance check
  docker compose run --rm pipeline make test lint
  docker compose run --rm pipeline-full make all    # needs the sibling repo's taxa.db
  make help                                          # every target
  ```
  **Python 3.14 everywhere**, matching the interpreter the committed numbers were produced
  under, so `make gold` in the image is a like-for-like reproduction rather than a comparison
  with a caveat. Verified: identical to every decimal, in the container and on the host.
  `apache/airflow:3.3.1-python3.14` exists; dbt-core needs **1.12.x** for 3.14 (not 1.10) and
  DuckDB needs **≥1.5**. Cosmos resolved fine at 1.15.1 despite its PyPI classifiers claiming
  only ≤3.12 — those are stale, which is worth remembering before trusting classifiers again.

  `uv` for locking, installed into `.venv` as an ordinary package (`make lock` / `make sync`).
  `uv.lock` is universal — one file resolving across 3.12/3.13/3.14 — which matters because
  `pip-compile` resolves only for the interpreter it runs under, and this project has three.
  `requires-python` moved to `>=3.12`: 3.10/3.11 were a claim CI never tested. The new `check`
  extra is pytest+ruff without Jupyter, so the image can verify itself without carrying it.
  **`scikit-learn` is pinned exactly (==1.9.0)**, not floored — `data/models/*_calibrator.pkl`
  are pickled `IsotonicRegression` objects and sklearn makes no cross-version pickle promise.

  The `dbt`/`tracking`/`airflow` extras are declared and locked but **not installed in the
  pipeline image**: they exist so one `uv lock` proves all four platform tools co-resolve on one
  interpreter before milestones 14-16 depend on it, and `Dockerfile.airflow` is where that proof
  actually runs. Installing them early cost ~600 MB of image for nothing; each milestone adds its
  own extra when it needs it.

  Four things had to be fixed to make any of this honest, all of them silent failure modes with
  a regression test each in `tests/test_paths.py` (both mutation-checked):
  1. **A real bug**: `_cache_is_valid()` called `taxa_db_path.stat()` unguarded, so a container
     with a prebuilt `lookup.sqlite` but no sibling `taxa.db` raised `FileNotFoundError` from
     inside a predicate — and `build_lookup_cache()` is called unconditionally from
     `features.py` and `candidates.py`. An absent source is not evidence of staleness; it now
     skips the comparison, keeps the column check, and errors with the mount name when it has
     neither a cache nor a source.
  2. `candidates.manifest.json` keyed on raw `st_mtime` floats — the only mtime-based manifest in
     the repo. Image layers, volume restores and fresh checkouts all rewrite mtimes without
     touching content. Now content fingerprints (`paths.file_fingerprint()`, sha256 for the same
     reason `_qid_set_fingerprint` uses it). Measured on the real data: 98s regenerate → 11s
     cache hit, and `touch`ing both sources no longer invalidates. Files over 64 MB are
     fingerprinted from size + head + tail (SQLite's change counter is in the first 100 bytes,
     parquet's metadata footer at the end); the fingerprint string records which mode was used.
  3. Same manifest **omitted** the `wikidata_parquet` key when that argument was not passed, and
     since the check is a dict comparison across differing key sets, a manifest written by
     `python -m src.candidates` could never match a call that left it out — a guaranteed silent
     rebuild. Both keys are now always present, `None` when absent.
  4. `os.cpu_count()` sized the worker pool from *host* cores regardless of a `--cpus` quota, and
     `processes` was reachable only from Python. Now `MATCHER_WORKERS` first — neither
     `cpu_count()` nor `process_cpu_count()` can see a CFS quota, so the env var is the only
     thing that actually works in a container.

  `src/paths.py` is the single source of truth for the fifteen path constants that each used to
  recompute their own (`MATCHER_DATA_DIR`, `MATCHER_TAXA_DB`, `MATCHER_MODEL_DIR`,
  `MATCHER_SIBLING_REPO`). **`MODEL_DIR` is overridable independently of `DATA_DIR`** on purpose:
  the frozen models are the one committed thing inside `data/`, and a volume mounted over `data/`
  would hide them — Docker seeds an *empty* named volume from the image, but a volume that
  already holds a previous run's output is not empty and never gets seeded.

  Two functions that were pipeline stages with no way to invoke them now have flags:
  `python -m src.wikidata --ancestors` and `python -m src.train --final`. All three flag-taking
  modules use `argparse`; `evaluate.py`'s old `"--gold" in sys.argv` accepted `--golf` silently
  and ran the wrong branch.

- **dbt over DuckDB (milestone 14)** — `dbt/` builds the same feature table `src/features.py`
  builds, in SQL, and writes it to `data/features_dbt.parquet`. 15 models, 103 tests, ~19s over
  the real 590,671-row frame.
  ```sh
  make features-sql   # dbt build --project-dir dbt --profiles-dir dbt
  make parity         # diff the two feature tables, column by column
  ```
  **Result: 46 of 52 columns identical.** The six that differ are `sim_rank_in_group` (38.25% of
  rows, every one inside a similarity tie), the three `*_match` columns plus their sum
  `shared_ancestor_depth` (1.4% of rows, 100% inside items whose P171 chain genuinely holds two
  ancestors at one rank), and `parent_name_jw` (0.67%, all non-ASCII). Written up in
  `docs/findings.md` §9 — that section is the milestone's actual deliverable, not the dbt project.

  The substitution §2.2 expected to dominate the drift barely registers: **DuckDB's `levenshtein`
  agrees with rapidfuzz exactly and `jaro_winkler_similarity` to 5.55e-17 (one ULP) on all 590,671
  rows.** But **both DuckDB functions count UTF-8 bytes where rapidfuzz counts code points** —
  `jaro_winkler_similarity('abc','ab×c')` is 0.689 against rapidfuzz's 0.933 — so
  `levenshtein_ratio`'s denominator uses `strlen` (bytes), not `length` (code points), to keep the
  units matching. Only `parent_name_jw` is exposed, being the one feature computed on raw rather
  than normalised names; every normalised name is ASCII because `normalize.py`'s genus/epithet
  patterns are `[A-Za-z-]`.
  Run from the repo root: dbt does **not** chdir into `--project-dir`, so `data/` in
  `dbt/profiles.yml` and in `fct_features`' `location` resolves against the caller's cwd. Every
  path derives from `MATCHER_DATA_DIR`, so pointing that at a throwaway directory is the whole of
  what `tests/test_dbt.py` does instead of maintaining a second profile that could drift.

  **It writes beside `data/features.parquet`, not over it** — a deliberate deviation from
  platform-design §5.2, which named the canonical path. The frozen milestone 6/7 models are quoted
  against the pandas artefact; overwriting it in the milestone that only *measures* the drift
  would destroy the thing the parity report compares to. Milestone 15 promotes it when it releases
  the freeze.

  Three more deviations, all in the same direction — keep the drift down to what is worth
  measuring:
  - **`normalize.py` is registered as a DuckDB UDF** (`src/dbt_udf.py`, a dbt-duckdb `Plugin`
    whose `configure_connection` calls `create_function`), not transcribed into `regexp_extract`
    as §5.2 proposed. It is a token-by-token state machine with `break` semantics, ten of the 41
    features derive from it, and a transcription would have buried the interesting drift under a
    tail of parser bugs. Returns a `STRUCT`; the fields must **not** be declared nullable
    (duckdb#18600 — it creates fine and then fails at call time). `null_handling='special'` so a
    NULL name reaches Python's degenerate-input branch instead of short-circuiting. Applied in
    `int_name_parts` to the ~650k *distinct* strings, not once per row per side.
  - **The 15% synthetic dropout stays the real seeded function** (`int_labels` is a Python model),
    against §4.3.2's plan to accept a hash-modulo selection. `label` and `no_answer_reason` are
    therefore identical between the two paths.
  - **`dim_folds` stays Python**, as §5.2 already intended: the leakage guarantee is the one
    property that must not move.

  `stg_link_findings` (the checker's `findings.db`) is deferred to milestone 16 — it has no
  consumer here, and a model that fails when the sibling repo is absent would break `dbt build`
  in the container.

  **A fourth order-dependency, not in §4.3's list of three:** `sim_rank_in_group` ranks with
  `.rank(method="first")` (`features.py:208`), so pandas breaks ties on `candidates.parquet`'s row
  order — which comes out of `imap_unordered` and is not stable across regenerations even on the
  pandas side. Ties are the common case, not the exception: every exact match scores 1.0.
  `int_group_stats` uses an explicit rule (similarity desc, then `inat_taxon_id`).

  Things worth knowing before touching the project:
  - **The attached SQLite index is addressed as a catalog**, `database: lookup` / `schema: main`
    in `sources.yml` — *not* `meta.external_location`, which quotes its value as a file path.
    That is right for the parquet sources and produces `Catalog Error: Table with name
    'lookup.taxa_normalized' does not exist` for an attached database.
  - **Generic test arguments go under `arguments:`** in dbt 1.12. The old inline form still runs
    but emits `MissingArgumentsPropertyInGenericTestDeprecation`, once per test.
  - `between` is a local generic test (`dbt/macros/test_between.sql`) rather than `dbt_utils`:
    a package would mean `dbt deps` and a network fetch before `dbt build` could run at all,
    which the five-minute path is built on not needing.
  - **Two `not_null` tests that looked obvious are wrong on real data.** `Q2125371` is a genuine
    Wikidata taxon with no label, so `stg_wd_taxa.wikidata_name` and `int_name_parts.raw_name` are
    both legitimately null, and `fct_features` joins the name parts with `is not distinct from`
    rather than `=` so that row's parse is reached instead of silently missed.
  - **`data/features.parquet`'s column order already disagrees with the code that writes it** —
    the on-disk artefact has the ten `strategy_*` columns alphabetically, `build_features()`
    emits them in `STRATEGY_TAGS` declaration order. Harmless, because `train.py` selects by name,
    but it is §4.3.3's positional-`monotone_constraints` hazard showing up for real.
    `fct_features` follows the code, and `build_parity_report.py` aligns by name.

- **Tests and CI** — `pytest` over `tests/`, plus `ruff check`. Both run in
  `.github/workflows/ci.yml` on Python 3.12, 3.13 and 3.14, alongside a `uv lock --check` job and
  a `docker` job that builds both images, runs the suite inside the pipeline image, and **greps
  `make gold`'s output for the committed numbers** — a silently-passing `make gold` would not
  catch a packaging regression, which is the whole thing milestone 13 is defending.
  ```
  make test
  make lint
  ```
  The suite never touches the network or `data/`. `tests/conftest.py` builds a real throwaway
  `taxa.db` from the 26 hand-written rows in `tests/fixtures/taxa_mini.csv` and runs the *real*
  `build_lookup_cache()` over it, so the SQL and the FTS5 trigram query are genuinely exercised
  rather than mocked. `tests/test_splits.py` is the leakage regression spec §7 milestone 4 asks
  for — previously only a `print` in `features.py`'s `__main__` — and it was mutation-checked
  (breaking `verify_no_qid_split_across_folds()` to always return `True` makes it fail).
  `ruff format` is deliberately **not** enforced: it would rewrite files that have never had a
  formatter applied and bury real changes in whitespace. `B905` (zip without `strict=`) is
  ignored in `pyproject.toml`, since every zip in `src/` walks columns of one DataFrame.

  Two things the tests pinned rather than fixed, both deliberate: `rubrum` → `ruber` is
  Levenshtein distance **3**, so strategy 2 does not reach gender variants (the
  `epithet_stem_match` feature handles them, and the trigram strategy still surfaces the row);
  and a ligature (`æ`/`œ`/`ß`) is not decomposed by NFKD, so it fails the genus/epithet character
  classes and the name parses empty or loses its epithet. There are zero such names in the 1.4M-row
  iNat index and changing normalisation would invalidate every cached feature the frozen models
  trained against, so it is recorded in `docs/future-work.md` instead.

- **Fixtures for the five-minute path** — `build_fixtures.py` regenerates
  `tests/fixtures/gold_*.csv.gz` + `oof_summary.json` from the full caches, so
  `python -m src.evaluate --gold` runs from a clean clone with no Node, no 189 MB download and no
  network. Rerun it after anything that changes the gold set.
  ```
  .venv/bin/python build_gold_set.py && .venv/bin/python build_fixtures.py
  ```
  `src/fixtures.py` holds the fallbacks; `features._load_inat_index()` and
  `evaluate._load_gold_attributes()`/`_load_gold_ancestors()`/`load_oof_reference()` prefer the
  real caches and announce which copy they used. Verified bit-for-bit: all 41 feature columns
  identical between the fixture path and the full path, and the printed metrics match to every
  decimal.

  **The iNat index fixture is not just the candidate rows.** It also carries every row *sharing a
  name* with a candidate (otherwise `n_inat_taxa_same_name`, which counts collisions across the
  whole index, would be silently wrong rather than absent) and every ancestor reachable from a
  candidate's `ancestry` string. ~4.8k rows of 1.4M.

  **The ancestor fixture is written unsorted, on purpose.** `_wd_ancestor_names_by_rank()`
  resolves an item's ancestor at each target rank first-wins, so when a transitive P171 chain
  contains two ancestors at the same rank (real, and not rare) row order decides the answer.
  Sorting that fixture changed `family_match`/`order_match` on 4 of 2,610 rows and moved
  `rank:map`'s gold top-1 by half a point. Caught by diffing feature frames between the two
  paths, which is the check worth repeating if the fixture ever stops matching.

  `.gitignore` gained an exception for `data/models/*.json` and `*_calibrator.pkl` (a directory
  excluded by `data/` cannot have its contents re-included, hence `data/*`). Those 4.4 MB are the
  frozen milestone 6/7 models; committing them is what makes the five-minute path real.

This section gets filled in further as the remaining milestones (§7) land, with the exact
runnable commands and their flags.

## Conventions

Python, per the spec's §0 repo shape (`src/`, `pyproject.toml`, `data/` gitignored except
`data/models/`, `gold/hard_cases.csv` committed).

**Where prose goes.** `README.md` is the 90-second read: what the classifier decides, the results
table, the three figures, the label-noise section, a milestone table, limitations, and the two run
paths. Reasoning and investigation go to `docs/findings.md`; the motivating Absidia narrative to
`docs/motivation.md`; anything deliberately not done to `docs/future-work.md`; the design of the
platform milestones (13-16) — tool versions and why, the audit of the existing pipeline, and the
alternatives that were rejected — to `docs/platform-design.md`. This file stays the
engineering log — long is fine here, not there. The README also declares that the code was written
with Claude Code as a pair programmer; keep that line, it is the honest framing.

**Versions are the changelog.** Adopted at milestone 14, matching `wikidata-inat-checker` and
`commons-describe-upload-toolbox`: bump `pyproject.toml`'s `version` on every feature or fix and
title the commit `vX.Y.Z: <what changed>`. No `CHANGELOG.md`, no tags, no release tooling —
`git log --grep '^v[0-9]'` is the changelog. A version bump means re-running `uv lock`, since the
project's own entry in `uv.lock` carries the version and CI's `uv lock --check` job will otherwise
fail. Commits before `v0.2.0` predate the convention and are left alone.

**Documentation stays current.** Update `README.md` and this file's Commands section in the same
commit as the code change they describe, not as a follow-up. A milestone isn't done until its
runnable command is documented here and reproducible from a clean checkout — that's also what
makes each milestone's "Check:" line in the spec verifiable by someone other than whoever wrote
the code.
