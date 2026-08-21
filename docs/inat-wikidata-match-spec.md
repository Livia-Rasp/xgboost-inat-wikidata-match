# iNat ↔ Wikidata taxon matching — project spec

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides whether a Wikidata taxon item and a
candidate iNaturalist taxon refer to the same taxon. Built on the data already indexed by
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker).

**Goal of the deliverable:** shrink the manual review queue that `npm run links` produces
(`output/links-ambiguous.html`) by auto-resolving the easy cases at a precision high enough
that nothing wrong gets written to Wikidata.

**Headline metric to aim for:** *"resolves N% of the ambiguous queue automatically at ≥99.5%
precision, versus M% for an exact-name-match rule."* Not AUC.

---

## 0. Repo shape

A **separate Python repo** (`xgboost-inat-wikidata-match`) rather than a folder inside the
Node project — cleaner as a portfolio piece, and it can read the existing SQLite cache
read-only. If it works, it can still be included as a feature into the Node project.

```
xgboost-inat-wikidata-match/
├── README.md              # results table, the negative-sampling story, model card
├── pyproject.toml
├── src/
│   ├── normalize.py       # scientific-name normalisation
│   ├── candidates.py      # candidate generation from the SQLite taxa index
│   ├── labels.py          # positives from Wikidata P3151
│   ├── features.py        # pairwise feature construction
│   ├── train.py           # XGBoost + CV + threshold selection
│   └── evaluate.py        # metrics, plots, gold-set scoring
├── data/                  # gitignored
├── notebooks/01-report.ipynb
└── gold/hard_cases.csv    # ~200 hand-labelled ambiguous pairs, committed
```

Inputs, both already available:

- **iNat side:** `~/.cache/wikidata-inat-checker/*.sqlite` — the taxa index built from
  iNaturalist's open-data `taxa.csv`. Columns available offline: `taxon_id`, `name`,
  `rank`, `rank_level`, `ancestry`, `active`. Open read-only.
- **Wikidata side:** batched SPARQL, same pattern the Node project already uses. Pull
  `P225` (taxon name), `P105` (rank), `P171` (parent taxon), `P3151` (iNat taxon ID),
  `P141` (IUCN), sitelink count, statement count, Commons category sitelink.

Target size: **30k–60k groups** is plenty. Keep the whole train run under a few minutes.

---

## 1. Name normalisation (`normalize.py`)

Do this once, consistently, on both sides. Everything downstream depends on it.

- lowercase; strip diacritics (NFKD → drop combining marks)
- strip authorship/year if present (`Rosa canina L.` → `rosa canina`)
- normalise hybrid markers: `×`, `x ` at token start → `hybrid_` flag, remove from string
- normalise infraspecific connectors: `subsp.`, `ssp.`, `var.`, `f.`, `cv.` → canonical tokens
- collapse whitespace and punctuation
- expose parsed parts: `genus`, `specific_epithet`, `infraspecific_rank`,
  `infraspecific_epithet`

Also expose a **gender-stripped epithet stem** (drop trailing `-us/-a/-um`, `-is/-e`,
`-er/-ra/-rum`). Latin gender agreement means *Acer rubrum* / *Acer ruber* style
disagreements are common and are real matches.

---

## 2. Candidate generation (`candidates.py`) — the important part

For each Wikidata taxon item, retrieve up to **K = 20** iNat candidates from the SQLite
index. The union of these strategies is what defines the problem:

1. **All** iNat taxa whose normalised name equals the Wikidata normalised name.
   *Do not stop at the first hit.* This is exactly where hemihomonyms live — `Prunella`
   is a mint (Lamiaceae) and a bird (Prunellidae); `Oenanthe` is a wheatear and a
   water-dropwort. These are the cases the model must earn.
2. Same genus, epithet within Levenshtein distance ≤ 2 (orthographic variants, gender
   agreement).
3. Same epithet, genus within distance ≤ 2 (genus transfers, misspellings).
4. Trigram similarity on the full normalised name, top 10.
5. Names from Wikidata `P1420` (taxon synonym) and basionym, run through 1–3.

Implementation note: back 1–4 with a normalised-name index table and a trigram table built
once from the SQLite taxa index, so generation is a lookup rather than a scan over 1.4M rows.

**Record `strategy` per candidate** — useful both as a feature and for error analysis.

---

## 3. Labels (`labels.py`)

- **Positive:** the candidate whose `taxon_id` equals the item's existing `P3151`.
- **Negatives:** every other candidate in the same group.

That's the whole trick: negatives are drawn from the *same distribution as inference time*.
Random negatives make the task trivial and the model useless.

Three things to handle explicitly, and to write up:

- **Label noise.** A chunk of P3151 statements were themselves added by bots doing exact
  name matching, so "ground truth" is partly the baseline you're trying to beat. Mitigation:
  prefer statements carrying a reference, and hand-check a random sample of 100. Report the
  observed error rate in the README rather than hiding it.
- **Groups with no correct answer.** Some Wikidata taxa have no iNat counterpart at all. The
  model must be able to abstain. Construct this case by **dropping the true candidate from a
  random 15% of groups**, leaving an all-negative group. Evaluate "correctly says none"
  separately.
- **Selection bias.** Items that already have P3151 skew toward well-known taxa. Report
  metrics stratified by obscurity (observation count, sitelink count) so the skew is visible.

**Splitting: `GroupKFold` on family, not on QID.** Sibling species within a family share
almost all their features; splitting on QID leaks.

---

## 4. Features (`features.py`)

All pairwise. Grouped by what they capture.

**String similarity**

| feature | notes |
|---|---|
| `name_exact_raw`, `name_exact_norm` | bool |
| `jaro_winkler_full`, `levenshtein_ratio_full` | on normalised names |
| `genus_exact`, `genus_jw` | |
| `epithet_exact`, `epithet_jw` | |
| `epithet_stem_match` | gender-agreement-insensitive |
| `token_count_diff`, `length_diff` | |
| `infra_rank_match`, `infra_epithet_match` | subsp./var. handling |
| `hybrid_flag_match` | |

**Taxonomic agreement** — this is what beats the string baseline

| feature | notes |
|---|---|
| `rank_equal`, `rank_level_diff` | iNat `rank_level`: 10 species, 20 genus, 30 family… |
| `kingdom_match` | the single strongest hemihomonym killer |
| `shared_ancestor_depth` | matching ranks from root down the iNat `ancestry` chain vs the Wikidata `P171` chain |
| `family_match`, `order_match` | |
| `parent_name_jw` | Wikidata `P171` label vs iNat parent name |
| `inat_active` | |

**Group context** — legitimate and powerful; compute within the candidate set

| feature | notes |
|---|---|
| `n_candidates` | group size |
| `n_inat_taxa_same_name` | homonym count for that name globally |
| `n_wikidata_items_same_name` | ambiguity on the other side |
| `sim_rank_in_group` | this candidate's rank by name similarity |
| `sim_margin_to_runner_up` | best-minus-second-best; often the top feature |
| `strategy` | one-hot of the generator that produced it |

**Popularity / quality**

| feature | notes |
|---|---|
| `log1p(inat_observation_count)` | optional: derive offline from open-data `observations.csv`, no API calls |
| `wikidata_sitelink_count`, `wikidata_statement_count` | |
| `wikidata_has_iucn`, `wikidata_has_commons_cat` | |

**Deliberately excluded (leakage):** anything derived from the P3151 link itself, any iNat
field that stores a Wikidata identifier, and any "curated/verified" flag.

*Optional, API-only:* taxon authorship. iNat's `taxa.csv` does not carry authorship, so an
`author_citation_match` feature would need per-taxon API calls. Skip for v1; note it in the
README as the highest-value feature you left on the table.

---

## 5. Model (`train.py`)

Start with binary classification:

```python
XGBClassifier(
    tree_method="hist",
    n_estimators=2000,
    learning_rate=0.05,
    max_depth=6,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    eval_metric="logloss",
    early_stopping_rounds=50,
)
```

Then try `rank:pairwise` with `group=` set to the candidate-set sizes, since the real task is
*pick one per item*. Report both; whichever wins, the comparison is worth a paragraph.

Nice touches, cheap to add:

- **Monotone constraints** — increasing on `jaro_winkler_full`, `shared_ancestor_depth`,
  `kingdom_match`. Costs a little accuracy, buys defensibility.
- **Calibration** — isotonic or Platt on a held-out fold, since the whole point is a
  threshold. Show a reliability diagram and Brier score.
- **SHAP** — one beeswarm plot, plus force plots for three interesting errors.

---

## 6. Evaluation (`evaluate.py`)

Report all of these. The first two matter most.

1. **Review-queue reduction at fixed precision.** Sweep the threshold, pick the lowest one
   where validation precision ≥ 0.995, report coverage there. Three bands:
   auto-accept / human review / reject.
2. **Baseline comparison.** Implement the honest baseline — exact normalised name match,
   tie-broken by observation count — and beat it on the same folds. If the model doesn't beat
   it, say so and investigate; a spec that admits this is more convincing than one that
   doesn't.
3. Group-level **top-1 accuracy** and **MRR**.
4. **Abstention accuracy** on the 15% no-true-candidate groups.
5. **Stratified metrics** by observation count decile and by kingdom.
6. **Gold set.** Hand-label ~200 rows sampled from `links-ambiguous.html` and use it as the
   real test set. This is the only evaluation not contaminated by bot-added labels, and it's
   the number an interviewer will trust. Commit it.
7. **Error taxonomy.** Bucket the residual errors — hemihomonyms, synonym chains, rank
   mismatches, subspecies collapse — with a couple of named examples each.

---

## 7. Milestones for Claude Code

Each with an acceptance check, so progress is verifiable.

1. **Ingest.** Read the SQLite taxa index read-only; build normalised-name and trigram
   lookup tables. *Check:* looking up `prunella` returns both the Lamiaceae and the
   Prunellidae taxon.
2. **Wikidata pull.** Batched SPARQL for 30k+ taxon items with P3151, cached to parquet.
   *Check:* re-run is a cache hit, no network.
3. **Candidate generation.** *Check:* true match present in the candidate set for ≥97% of
   positive groups (this recall ceiling caps everything downstream — measure it and put the
   number in the README).
4. **Features + splits.** *Check:* `GroupKFold` on family, no QID appears in two folds.
5. **Baseline.** Exact-match rule scored on the same folds.
6. **Model + threshold selection.** *Check:* precision-at-threshold table reproduces.
7. **Gold set** hand-labelled and scored.
8. **Balance the gold sample across the alphabet.** The initial 476-item ambiguous sample turned
   out to cover only names starting A-C (root cause: `wikidata-inat-checker`'s `allNames()` runs
   an unordered `SELECT DISTINCT` that happens to come out alphabetically sorted, and `--limit`
   caps collected candidates before the scan ever reaches past the C's — see `gold/README.md`).
   Come up with a way to pull ambiguous candidates spanning the rest of the alphabet too. Doesn't
   need to be exhaustive or proportional per letter — a modest number from each of the letters
   currently missing is enough to stop the gold set being systematically skewed toward an
   arbitrary alphabetic slice. *Check:* the gold sample's first-letter distribution covers
   materially more than A-C (not necessarily uniform, just not concentrated in one narrow range).
9. **Discuss and finetune the results.** Review every gold-set top-1 miss individually against
   its full feature/score breakdown, not just the aggregate accuracy/MRR — this is how labeling
   errors get caught (a mislabel surfaced and got corrected this way on the very first partial
   run) and how genuine model gaps get told apart from labeling noise, iNat-side data issues, or
   evaluation-methodology artifacts. Any resulting model change (e.g. an extended monotone
   constraint) needs a fair before/after comparison across the whole gold set, not a decision
   made off a single anecdote — and should wait until enough gold labels exist for that
   comparison to be meaningful. *Check:* every miss on the final gold run has a written
   characterization (close call / labeling error / model gap / other), and any adopted model
   change shows an improvement on the full gold set, not just the case that motivated it.
10. **QuickStatements export.** A script that reads `gold/hard_cases.csv`'s confirmed matches
    (`label == 1`) and writes one `{qid}\tP3151\t"{inatId}"` line each to a `.qs` file, ready to
    paste into [QuickStatements](https://quickstatements.toolforge.org/) as a single batch import
    — this project's actual real-world deliverable, not just an evaluation artifact; hand-labeling
    the gold set already resolves taxa an automated match couldn't, so the resolved links should
    go back into Wikidata rather than sitting unused in a CSV. *Check:* line count matches the
    confirmed-match row count in `gold/hard_cases.csv`.
11. **README** with the results table, the negative-sampling story, the label-noise
    disclosure, and a short model card.
12. *(Optional)* `score_ambiguous.py` that reads `links-ambiguous.html` rows and emits
    accept/review/reject — closing the loop back into the Node tool.

---

## 8. What makes this interesting to showcase

Three things, worth making explicit in the README:

- Negatives are sampled from the deployment distribution, and the write-up explains why
  random negatives would have inflated every number.
- The metric is a decision under asymmetric cost, not accuracy — a wrong write to Wikidata is
  much worse than a deferral to a human.
- The label noise is disclosed and quantified rather than assumed away.
