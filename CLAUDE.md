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
  .venv/bin/python build_gold_labeling_kit.py       # samples up to 500, writes the labeling kit
  # (hand-label gold/labeling_template.csv -> save as gold/labeling_filled.csv)
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

- **Balance the gold sample across the alphabet (milestone 8, not started)** — the 476-item
  ambiguous sample generated so far covers only names from "Abietinella" to "Cattleya" (confirmed:
  296/92/88 split across A/B/C, nothing D-Z). Root cause traced to `wikidata-inat-checker`:
  `allNames()` (`lib/getInatTaxaDb.js`) runs `SELECT DISTINCT name FROM taxa` with no `ORDER BY`,
  but SQLite's `DISTINCT` implementation happens to produce alphabetically-sorted output as a side
  effect; `checkLinks.js` scans names in that incidental order, and `--limit 80000` caps collected
  candidates before the scan ever reaches past the C's — not sampling noise, a systematic
  artifact. Doesn't need to be exhaustive or proportional per letter, just not concentrated in one
  narrow alphabetic slice. Options not yet decided between: raise `--limit` further (more WDQS
  exposure, the exact failure mode `--ambiguous-only` was built to reduce), randomize the scan
  order before applying `--limit` (smaller, more surgical change), or run separate bucketed scans
  per alphabet range. Whichever approach, new items need to be sampled into
  `gold/labeling_filled.csv` alongside (not replacing) the existing 476 A-C answers — same
  "reorder without overwriting edits" concern already solved once for the alphabetical-sort fix.
  See `gold/README.md`'s "Known limitation" note.

- **Discuss and finetune the results (milestone 9, ongoing alongside labeling)** — spec gained
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

  Both findings are parked, not implemented — `src/train.py` and `data/models/`/
  `data/oof_predictions.parquet` were restored to their pre-experiment state after testing
  finding 1. Revisit both together once labeling is further along than the initial 50 items;
  finding 2 first, since it's a low-risk metric-computation fix that doesn't need retraining and
  makes any future finding-1-style comparison honest in the first place.

  **At n=192** (up from 50; 192/476 sampled items answered, still A-C only per milestone 8):
  `top1_accuracy`/MRR are binary 98.2%/0.989, rank 85.9%/0.928, baseline 20.8% — the binary-vs-
  rank gap *widened* rather than narrowed (was 95.7%/93.5% at n=50), including on the non-trivial-
  by-rank bucket specifically (98.5% vs. 82.3%). Not yet investigated whether finding 2 above
  explains part of this — noted for when milestone 8 work resumes, per Livia's call. Recall
  ceiling corrected to 99.41% (one genuine candidate-generation miss, `Q4694188`) after fixing a
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
