# xgboost-inat-wikidata-match

[![CI](https://github.com/Livia-Rasp/xgboost-inat-wikidata-match/actions/workflows/ci.yml/badge.svg)](https://github.com/Livia-Rasp/xgboost-inat-wikidata-match/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides
which iNaturalist taxon a Wikidata taxon item refers to, when the name alone is ambiguous.

[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker) scans Wikidata taxa
against iNaturalist's open-data taxon dump and writes everything it cannot resolve on a unique
name match to a human review queue. One scan of 80,000 names produces 491 such items. On a
hand-labelled sample of 263 of them, this model ranks the correct iNat taxon first **98.7%** of
the time, against **20.9%** for the exact-name-match rule the queue currently relies on. That
turns a queue item from "search iNaturalist and compare ancestries" into "confirm or reject one
suggestion".

> Built with [Claude Code](https://claude.com/claude-code) as a pair programmer; `CLAUDE.md` is
> the working log kept for it. The project design, the label-noise hypothesis, the gold-set
> methodology, and every hand-label in `gold/` are mine.

## Results

| | Answers without a human¹ | Precision of those answers | Top-1 accuracy² | MRR |
|---|---|---|---|---|
| Exact-match baseline (OOF) | 87.0% | 80.3% | 81.2% | — |
| `binary:logistic` (OOF) | 0.002% | 100% | **99.1%** | 0.995 |
| `rank:map` (OOF) | none³ | — | 99.0% | 0.994 |
| Exact-match baseline (gold) | 100% | 20.9% | 20.9% | — |
| **`binary:logistic` (gold)** | none³ | — | **98.7%** | **0.993** |
| `rank:map` (gold) | none³ | — | 97.8% | 0.989 |

¹ Different units, same question: how often can this run unsupervised, and how often is it right
when it does. For the baseline it is the share of items where an exact name match exists at all;
for the models, the share of candidate rows clearing a threshold chosen for ≥99.5% precision. The
baseline answers far more often and is wrong a fifth of the time.
² Was the correct candidate ranked first. For the baseline this counts a correct abstention as
correct too, so it is not deflated by items with no answer.
³ No threshold reaches 99.5% precision. This is a real result and it is discussed in
[Limitations](#limitations), not a missing measurement.

**OOF** is 5-fold out-of-fold cross-validation over 590,671 candidate rows for 58,842 Wikidata
items, grouped on family so no item's rows span two folds. **Gold** is 263 ambiguous Wikidata
items with no P3151 statement, hand-labelled for this project — a population no bot has ever
touched, and disjoint from everything the model trained on. Candidate generation finds the
correct taxon for **100%** of the gold items that have one, so nothing above is capped by recall.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/calibration-dark.png">
  <img alt="Reliability diagram: the raw model score sags far below the perfect-calibration diagonal, with 50,296 rows scoring above 0.95 of which only 83.9% are correct; the isotonic-calibrated score sits on the diagonal." src="docs/img/calibration-light.png">
</picture>

## The label-noise finding

Isotonic calibration surfaced something the accuracy numbers hide. 50,296 candidate rows score
≥0.95 on the raw model probability, but only **83.9%** of them are correct, which is why the
strict 99.5%-precision auto-accept band covers just 10 rows out of 590,671. Inside that
overconfident cluster, correct and incorrect rows are statistically indistinguishable on every
engineered feature, and it is almost never a genuine tie between two candidates. That pattern
does not look like a weak model; it looks like wrong labels. Training labels come from Wikidata's
P3151 statements, most of them added in bulk by bots, so the hypothesis was that the model was
being marked wrong for getting the answer right.

Testing that needed labels P3151 never touched, which is the reason the gold set exists at all.
On the gold set, the same raw-score band reads **98.2%** precision instead of 83.9%. The ceiling
was in the labels.

Full working, including the checks that ruled out a feature gap and a tie-breaking gap, is in
[`docs/findings.md`](docs/findings.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/threshold-bands-dark.png">
  <img alt="Both 99.5%-precision bands drawn to scale: 91.6% of candidate rows confidently rejected, 8.4% left for human review, and an auto-accept band of 10 rows too small to see." src="docs/img/threshold-bands-light.png">
</picture>

## How it is evaluated

Three things make these numbers mean what they say:

- **Negatives come from the deployment distribution.** A negative here is another candidate that
  survived generation for the same Wikidata item, not a taxon drawn at random from the 1.4M-row
  index. Random negatives are trivially separable and would have inflated every number in the
  table. This is why the baseline scores 20.9% on gold rather than something respectable.
- **The metric is a decision under asymmetric cost.** A wrong write to Wikidata is much worse
  than a deferral to a human, so thresholds target 99.5% precision and the honest answer is
  sometimes "this system should not act unsupervised". Not AUC.
- **The label noise is quantified, not assumed away.** See above.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/shap-summary-dark.png">
  <img alt="SHAP beeswarm: name_exact_raw dominates, followed by jaro_winkler_full and sim_margin_to_runner_up." src="docs/img/shap-summary-light.png">
</picture>

## Milestones

Spec §7's checkable list. Every "key number" below is reproduced by the command in
[Reproducing this](#reproducing-this).

| # | What it does | Key number | |
|---|---|---|---|
| 1 | Normalised-name + FTS5 trigram index over the local iNat taxa dump, read-only | 1,413,946 taxa indexed | done |
| 2 | Batched SPARQL pull of Wikidata taxa carrying P3151 | 58,874 items | done |
| 3 | Candidate generation, five strategies, K=20 | recall 99.84% of resolvable items | done |
| 4 | Features + `GroupKFold` on family | 590,671 rows × 52 features, no QID in two folds | done |
| 5 | Exact-match baseline, tie-broken by observation count | 81.2% accuracy | done |
| 6 | Two objectives, isotonic calibration, threshold selection | 99.1% top-1 OOF | done |
| 7 | Hand-labelled gold set of ambiguous, no-P3151 items | 98.7% top-1, n=263 | done |
| 8 | Fix the alphabetic bias in the gold sample | A–Z coverage, 491 items found | done |
| 9 | Per-miss review, and picking between the two objectives | `binary:logistic` picked | done |
| 10–12 | QuickStatements export, loop back into the Node tool | — | [future work](docs/future-work.md) |
| 13–16 | Docker, dbt-core over DuckDB, MLflow, Airflow + Terraform | — | planned |

Milestones 1–12 build the model. 13–16 are platform work — a container, a SQL transformation
layer, experiment tracking and an orchestrated DAG — and are not intended to make the model
better; see spec §7 for what each one has to demonstrate.

The reasoning behind milestones 6, 7 and 9 is in [`docs/findings.md`](docs/findings.md); the full
per-milestone breakdowns and plots are in
[`notebooks/01-report.ipynb`](notebooks/01-report.ipynb).

## Limitations

Stated plainly, because a reviewer who finds an undisclosed limitation should discount the rest
of the numbers.

- **Neither model can act unsupervised at the precision bar this task needs.** At ≥99.5%
  precision the auto-accept band covers 10 of 590,671 OOF rows and zero gold rows. The system
  ranks well; it does not yet decide.
- **The reject threshold does not transfer.** Fitted on OOF data it is 99.6% precise and rules
  out 91.6% of candidate rows. Re-applied unchanged to the gold set it drops to 95.7%, and would
  hide the true match for 107 of 263 items. Thresholds fitted on the P3151 population do not hold
  on the ambiguous one, which is the population that matters.
- **The gold set is 263 items.** It covers A–Z after milestone 8, but the
  `binary:logistic`-versus-`rank:map` decision rests on a two-item difference in top-1 (three
  misses against five, over the 230 items that have a correct answer). The pick is made on the
  best evidence available and on a consistent direction across every metric, not on a
  large-sample result. 620 sampled items remain unlabelled.
- **Training labels are noisy.** Quantified above, not eliminated. Every OOF number in this repo
  inherits it.
- **12.85% of P3151 links are stale**, pointing at iNat taxon IDs that no longer exist as active
  taxa. They are unreachable by any candidate strategy, so the raw recall ceiling reads 87.0%
  against 99.84% among resolvable items. Both numbers are reported everywhere rather than the
  flattering one.
- **Two of the three remaining gold misses are iNat-side duplicate records**, not ranking
  failures. The headroom left on this sample is mostly not in the model.

## Reproducing this

### The five-minute path

Runs the gold-set evaluation end to end against committed fixtures. No Node, no 189 MB download,
no network.

```sh
git clone https://github.com/Livia-Rasp/xgboost-inat-wikidata-match.git
cd xgboost-inat-wikidata-match
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m src.evaluate --gold
```

This reproduces the gold rows of the results table, the label-noise band comparison, and the
per-miss breakdown. It works because `gold/hard_cases.csv`, the two small Wikidata pulls for
those items, the slice of the iNat index they touch, and the frozen models are all committed.
Everything else in `data/` is gitignored and rebuilt by the full path below.

### The full path

Needs `~/.cache/wikidata-inat-checker/taxa.db`, which this repo reads read-only and never builds.
Produced by running any checker in the sibling repo:

```sh
git clone https://github.com/Livia-Rasp/wikidata-inat-checker.git
cd wikidata-inat-checker && npm install    # Node.js 26+
npm run links                              # downloads ~189 MB, builds a ~236 MB index, once
```

Then, from this repo, in order:

```sh
.venv/bin/python -m src.wikidata     # milestone 2: batched SPARQL, ~30 requests, cached
.venv/bin/python -m src.candidates   # milestones 1+3: builds the lookup cache, generates candidates
.venv/bin/python -m src.features     # milestone 4: ancestor pull (~8 min, network) + features
.venv/bin/python -m src.evaluate     # milestone 5: baseline, per fold and overall
.venv/bin/python -m src.train        # milestone 6: both objectives, 5-fold OOF, thresholds
.venv/bin/python build_figures.py    # regenerates docs/img/ from the caches above
```

Every step caches to `data/` with a manifest and is a no-op on rerun unless its inputs change.
Total first-run cost is roughly 15 minutes, most of it waiting on Wikidata Query Service. The
exact flags, cache-invalidation rules, and the failure modes worth knowing about are documented
per milestone in [`CLAUDE.md`](CLAUDE.md).

The gold-set workflow — generate a fresh ambiguous sample, hand-label it, score it — is in
[`gold/README.md`](gold/README.md).

### Tests

```sh
.venv/bin/python -m pytest      # runs against committed fixtures, no network
.venv/bin/ruff check .
```

## Design

- [`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md) — the full spec: repo
  shape, normalisation rules, candidate strategies, feature set, model config, milestones.
- [`docs/motivation.md`](docs/motivation.md) — why ambiguous P3151 links matter, and what a
  wrong one looks like on a live iNaturalist page.
- [`docs/findings.md`](docs/findings.md) — the calibration investigation, the threshold-transfer
  result, every gold-set miss characterised, and why `binary:logistic` won.
- [`docs/platform-design.md`](docs/platform-design.md) — the design for milestones 13–16: tool
  versions and why, an audit of the existing pipeline, and the alternatives that were rejected.
- [`docs/future-work.md`](docs/future-work.md) — what is deliberately not done yet.

The milestone 6 and 7 models in `data/models/` are frozen. They are the fixed reference point
every number here is quoted against, and `train.build_final_models()` only retrains when a model
file is missing or `force_refresh=True` is passed. Hand-labelling the gold set adds new P3151
statements to Wikidata, so a future re-pull would see a different population than the one these
models trained on. Freezing them means that drift cannot silently change the results.

## Licence

[MIT](LICENSE).
