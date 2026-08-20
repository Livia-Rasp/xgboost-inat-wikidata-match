# xgboost-inat-wikidata-match

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides
whether a Wikidata taxon item and a candidate iNaturalist taxon refer to the same taxon. Built
on the data already indexed by
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker), with the goal of
shrinking the manual review queue its `npm run links` command produces.

Full specification: [`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md).

## Status

Milestones 1–4 of the spec's §7 list are done.

- **Ingest** (`src/normalize.py`, `src/candidates.py`): name-normalisation rules from spec §1,
  and a cached normalised-name + FTS5 trigram lookup table built from the local iNat taxa index,
  read-only. Also excludes ~4.5k provisional/unresolved iNat names (no parseable epithet) from
  the candidate index — see milestone 3 below.
- **Wikidata pull** (`src/wikidata.py`): batched SPARQL, `LIMIT`-capped to 60,000 taxa with
  P3151 set (of 856,040 that actually have it), plus P1420/P566 synonym and basionym names,
  cached to parquet.
- **Candidate generation** (`src/candidates.py`): the five strategies from spec §2, capped at
  K=20 per item, cached to `data/candidates.parquet`. **Recall @ K=20: 87.01%** raw (spec target
  ≥97%) — but 12.85% of P3151 links point to iNat taxon_ids that no longer exist as active taxa
  (stale/deactivated references, a data-quality issue, not a generation gap); recall among the
  resolvable items is **99.84%**.
- **Features + splits** (`src/labels.py`, `src/features.py`): all of spec §4's feature groups
  (string similarity, taxonomic agreement, group context, popularity/quality — `kingdom_match`
  and `family_match` needed extending `src/wikidata.py` with a full Wikidata ancestor-chain pull,
  since milestone 2 only had one hop of P171), labels from P3151 with spec §3's 15% synthetic
  abstention dropout, and `GroupKFold(n_splits=5)` on family. **Check passes: no QID appears in
  two folds.** Cached to `data/features.parquet` (590,671 rows × 52 columns).

Full breakdowns and plots for every milestone in
[`notebooks/01-report.ipynb`](notebooks/01-report.ipynb).

No other milestone is implemented yet.

## Install / run

### Prerequisite: the iNat taxa index

This repo reads `~/.cache/wikidata-inat-checker/taxa.db` read-only — it doesn't build it. That
cache is produced by [wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker)
the first time one of its checkers runs. On a machine that doesn't have it yet:

```sh
git clone https://github.com/Livia-Rasp/wikidata-inat-checker.git
cd wikidata-inat-checker
npm install    # Node.js 26+
npm run links  # or any other checker — first run downloads and builds the index
```

Downloads iNaturalist's ~189 MB open-data taxon dump and builds a ~236 MB SQLite index at
`~/.cache/wikidata-inat-checker/taxa.db`; takes a couple of minutes once, refreshed every 30
days. `npm run links` is the one worth running here since it also produces
`output/links-ambiguous.html` — the review queue this project exists to shrink.

### This repo

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

**Ingest check (milestone 1).** Reads `~/.cache/wikidata-inat-checker/taxa.db` read-only and
builds a cache at `data/lookup.sqlite` (gitignored — first run ~20s over 1.4M rows, cached
after), then looks up `prunella`, the spec's acceptance check: a genus name shared by a bird
family (Prunellidae) and a mint family (Lamiaceae) that only differ by ancestry. Run from the
repo root; needs the venv (`pandas`, `rapidfuzz` — milestone 3 added these as module-level
imports, so plain `python3` no longer suffices here).

```
.venv/bin/python -m src.candidates
```

Expected output (first line only — the rest is milestone 3's full candidate-generation run,
below):

```
2 match(es) for 'prunella':
  {'taxon_id': '13982', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/1/2/355675/3/7251/71358'}
  {'taxon_id': '52765', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/47126/211194/47125/47124/48151/48623/520502/918917/919181'}
```

**Wikidata pull (milestone 2).** Batched SPARQL against `https://query.wikidata.org/sparql` for
taxa carrying P3151, cached to `data/wikidata_taxa.parquet` (+ a sidecar manifest that the
cache-hit check compares against, gitignored). First run takes a few minutes and makes ~30
batched network requests; reruns are a cache hit.

```
.venv/bin/python -m src.wikidata
```

**Candidate generation (milestone 3).** Runs the ingest check above, then generates up to 20
candidates per Wikidata item via the five strategies in spec §2, cached to
`data/candidates.parquet`, and reports the recall-ceiling breakdown. Pure local SQLite/CPU work
(no network) — parallelized across `os.cpu_count()` worker processes (capped at 16); ~1-2
minutes for the full 58,874-item pull here.

```
.venv/bin/python -m src.candidates
```

Expected output (after the `prunella` check above):

```
590,671 candidate rows for 58,874 Wikidata items (101.6s)
raw recall @ K=20: 87.01% (spec target: >=97%)
  7,564/58,874 (12.85%) true P3151 links point to iNat taxon_ids that don't exist in the active-taxa index (stale/deactivated) -- unreachable by any strategy, not a generation gap
  recall among the 51,310 resolvable items: 99.84%
```

**Features + splits (milestone 4).** Pulls each Wikidata item's full ancestor chain (needed for
`kingdom_match`/`family_match`/`order_match`; cached separately to
`data/wikidata_ancestors.parquet`, ~8 min one-time network cost, ~2.7M rows), builds labels
(P3151 positives + spec §3's 15% synthetic abstention dropout), computes every feature in spec
§4, and splits `GroupKFold(n_splits=5)` on family — grouping on the Wikidata item's *own*
ancestor chain, not the candidate's, so a WD item's rows always land in one fold regardless of
which candidate turns out correct.

```
.venv/bin/python -m src.features
```

First run: ~8 min (network, one-time) + <1 min (local). Reruns of just this step are instant
cache hits; the ancestor pull only re-runs if the Wikidata item set changes.

Expected output (local part only — after the ancestor pull, which prints its own progress):

```
590,671 feature rows, 52 columns
no QID split across folds: True

fold sizes:
fold
0    11229
1    11121
2    12610
3    13804
4    10078
Name: wikidata_qid, dtype: int64

no_answer_reason breakdown (per item):
no_answer_reason
has_positive         43543
synthetic_dropout     7684
stale_p3151           7615
Name: count, dtype: int64

distinct family_key groups: 4,679
```

No other commands exist yet — this section grows as the remaining milestones (§7) land.
