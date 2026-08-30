# Findings

What the numbers turned out to mean, and how each conclusion was checked. Every figure here is
reproducible with the commands in [`../README.md`](../README.md); the plots come from
`build_figures.py`.

---

## 1. The model was overconfident, and the reason was the labels

Isotonic calibration on the out-of-fold scores surfaced a gap that a plain accuracy number hides
completely. 50,348 candidate rows score ≥0.95 on the raw `binary:logistic` probability, but only
**83.9%** of them are actually correct. Calibration does the right thing and compresses that
whole cluster down to ~84%, which is why the strict 99.5%-precision auto-accept band ends up
covering **6 rows out of 590,671**.

That could mean the model is bad, the features are too weak, or the labels are wrong. Three
checks separated those:

1. **Is it a feature gap?** Inside the ≥0.95 cluster, correct and incorrect rows are
   statistically indistinguishable across every engineered feature. Nothing in the feature set
   separates them, which is not what a "needs a better feature" failure looks like — that would
   show as *some* feature shifting between the two groups.
2. **Is it an unresolved tie?** Almost never: 14 of 50,282 items in that cluster have more than
   one candidate scoring in the band. The model is not torn between two options; it is confident
   and marked wrong.
3. **Are the labels right?** This is the one that needed new data. Training labels come from
   Wikidata's P3151 statements, most of which were added in bulk by bots. If a meaningful slice
   of those is wrong, a model that learned the *correct* answer gets scored as incorrect.

The hypothesis was that (3) explains the gap. Testing it needed labels P3151 never touched,
which is the entire reason the gold set exists.

**Result:** on the hand-labelled gold set, the same raw-score band reads **98.3% precision**
(n=173) against 83.9% on the P3151 population. The ceiling was in the labels, not the model.

This is the most durable result in the project. Milestone 15 retrained five separate model
versions (§10) and re-measured the band against each: the gold side stayed between 96.9% and
98.3% throughout, while the OOF side barely moved from 83.9%. The gap is a property of the two
populations, not of any one fitted model.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/calibration-dark.png">
  <img alt="Reliability diagram of raw versus isotonic-calibrated scores." src="img/calibration-light.png">
</picture>

This does not mean P3151 is 14 points wrong. The gold set is a different, harder population
(ambiguous items by construction) and 173 rows is a small sample. It does mean the
overconfidence measured against P3151 cannot be read as a model deficiency, which is what the
raw number would otherwise imply.

---

## 2. The reject threshold survives the population change for one objective, and not the other

*Rewritten at milestone 15. This section previously reported the failure as a property of the
task. It is a property of `binary:logistic`, and the reason only that half was visible is that
`binary` was the reported default and nobody had run the comparison on `rank:map`.*

Both thresholds are chosen on OOF data at 99.5% precision. On that population the reject side does
almost all of the useful work: it rules out **91.8%** of candidate rows for `binary` and **91.4%**
for `rank`, at 99.50% and 99.75% row-level negative precision respectively.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/threshold-bands-dark.png">
  <img alt="The reject, review and auto-accept bands drawn to scale." src="img/threshold-bands-light.png">
</picture>

Re-applied to the gold set, unchanged, the two come apart completely:

| re-applied to gold, n=263 | `binary:logistic` | `rank:map` |
|---|---:|---:|
| row-level negative precision | 96.0% | **99.79%** |
| items ruled out | 131 | 33 |
| **items whose true match is hidden** | **98** | **5** |

`binary:logistic`'s threshold is unusable on the deployment population: it would discard the
correct answer for more than a third of the queue. `rank:map`'s holds — it clears 12.5% of the
queue and loses five items doing it, at a row precision *above* the 99.5% it was fitted for.

This is the largest practical difference between the two objectives, and it is most of why
`rank:map` is the reported default (§6, §10). It does not make the caution obsolete: a threshold
fitted on the P3151 population still has to be *checked* on the ambiguous one before it is
trusted, and here that check passes for one objective and fails badly for the other. The
auto-accept side transfers no better for either: zero gold rows clear it.

---

## 3. Within-group ranking has to use the raw score, not the calibrated one

Isotonic regression is a step function. It maps whole ranges of distinct raw scores onto a
single output value, which is exactly right for anything that needs cross-group comparability
(thresholds, Brier score) and destroys the information that top-1 accuracy and MRR depend on:
the *relative order* of candidates inside one group. When a plateau swallows a whole group, the
tie breaks on row order instead of on the model's actual preference.

At OOF scale this barely registers — 0.9912 raw vs 0.9915 calibrated for `binary:logistic`,
0.03pp — because a group rarely lands entirely inside one plateau across 590,671 rows. On the
gold set it was worth 11 points:

| gold set, n=263, as measured on the frozen models | ranked by calibrated prob | ranked by raw score |
|---|---|---|
| `binary:logistic` top-1 | 98.7% | 98.7% |
| `rank:map` top-1 | 86.5% | **97.8%** |

`rank:map` was the only casualty, and predictably so: its raw scores are unbounded ranking
margins rather than probabilities, so isotonic regression compresses them far harder and its
plateaus are correspondingly wider. `binary:logistic` is unaffected to four decimal places.
Reported the wrong way, `rank:map` looked like a much worse model than it is — and the earlier
partial gold runs, which reported 85.9%, were reading this artifact rather than a real
12-point gap. Both `src/train.py` and `src/evaluate.py` now rank on the raw score and keep calibrated
probabilities for probability-quality metrics only; see `evaluate.ranking_score_column()`.

---

## 4. Monotone constraints: verified, and one extension left on the table

`jaro_winkler_full`, `shared_ancestor_depth` and `kingdom_match` are constrained monotone
increasing. This costs a little accuracy and buys defensibility: a candidate can never be scored
*less* plausible for scoring higher on any of them. The constraints are built programmatically
from `FEATURE_COLUMNS` (`train.monotone_constraints_tuple()`) rather than as a hardcoded
position tuple, and were verified live by sweeping each feature — zero violations.

`sim_rank_in_group` and `family_match` are *not* constrained, and the gold set found a case where
that matters. For `Q106123971` (*Acmella pusilla*), `rank:map` ranked a candidate with strictly
worse taxonomic agreement (`kingdom_match`, `family_match` and `shared_ancestor_depth` all worse)
above one with better agreement. `binary:logistic` got the same case right.

Extending the constraints was tested and does fix that ordering. It was **reverted rather than
adopted**: it needs a fair before/after across the whole gold set, and retraining would also
break the deliberate freeze on the milestone 6/7 models that every number in this repo is quoted
against. Left as future work, not as a silent pending change.

---

## 5. Every gold-set top-1 miss, characterised

Spec §7 milestone 9 requires each miss to get a written reading rather than being counted into an
aggregate. `src/evaluate.py --gold` prints them with their full feature breakdown.

**Reviewed again after milestone 15's retrain.** The champion misses three items per objective,
two of them shared. The two misses that were new to the champion are the last two rows of the
table; reviewing them produced a third labelling correction and one genuine model weakness.

At n=263 on the frozen models, `binary:logistic` missed 3 items and `rank:map` missed 5.

| Item | Picked | Correct | Reading |
|---|---|---|---|
| `Q4694188` *Agrisius japonicus* | `1265680` | `1265680` | **Was a labelling error.** Recorded as `265680` — a dropped leading digit, landing on *Poecilopompilus algidus*, a spider wasp that was never among the offered candidates. Corrected. It was the sole reason the gold recall ceiling read 99.57% instead of 100%. |
| `Q121887868` *Acanthocephala* | `61446` (genus) | `151826` (phylum) | **Model gap.** The Wikidata item is a phylum and only the correct candidate matches that rank, yet the model ranked the identically-named genus first. The one miss in the trivial-by-rank bucket. |
| `Q25661362` *Auricula* (`rank:map` only) | `742765` (section) | `1269419` (genus) | **Model gap, same shape as above.** `binary:logistic` gets it right. |
| `Q16760098` *Utetheisa albilinea* | `1522832` | `1550468` | **iNat data issue.** Two iNat records for what appears to be the same taxon, differing only by an extra subgenus in the ancestry. Either answer is defensible; the labeller's note says as much. |
| `Q46674974` *Bursaria* | `83575` | `1051126` | **iNat data issue.** Two genus records with incomplete ancestry, so no taxonomic feature can separate them. |
| `Q106123971` *Acmella pusilla* (`rank:map` only) | `157976` | `1349465` | **Model gap specific to `rank:map`** — the unconstrained-interaction case in §4. |
| `Q14908802` *Joziratia anjouanae* | `1609619` | `1609619` | **Was a labelling error — the model was right.** Two iNat records share the name and are identical on **all 41 features except `sim_rank_in_group`**, so the pick was decided entirely by the tie-break rule. Originally labelled `1609620` on GBIF ancestry concordance; `1609620` is **inactive** on iNaturalist, which settles it. Both are still in the local index because `taxa.db` is a snapshot taken before the deactivation. Corrected. |
| `Q20668495` *Afrocrania* (`rank:map` only) | `1642969` (genus) | `1296985` (subgenus) | **Genuine model gap, and the worst kind for this project: a hemihomonym.** Wikidata's *Afrocrania* is a plant genus (parent *Cornaceae*); iNat holds the plant as a **subgenus** and a different *Afrocrania* — an animal — at **genus** rank. So `rank_equal` points at the animal while `kingdom_match`/`family_match`/`order_match` and `shared_ancestor_depth` all point at the plant. `binary:logistic` weighs the taxonomy and gets it right; `rank:map` weighs `rank_equal` and does not. v5's extended constraints do **not** fix it — checked directly. |

Two of the three surviving `binary:logistic` misses are iNat-side duplicate records rather than
ranking failures. That is worth knowing before chasing the last point of accuracy: the remaining
headroom on this sample is mostly not in the model.

**The per-miss review has now caught a labelling error on all three runs it has been done on** —
`Q21438872` on an early 50-item run, `Q4694188` at n=263, and `Q14908802` here. It is cheap and it
keeps paying, and it is the reason a label correction is folded into §10's table rather than
discovered later.

**The two objectives fail in mirror-image ways**, which is the most useful thing this round of the
review produced. `rank:map` over-weights `rank_equal` and loses `Q20668495`, a hemihomonym where
every taxonomic feature points the other way. `binary:logistic` under-weights it and loses
`Q121887868`, where the Wikidata item is a phylum and `rank_equal` is the only signal that
separates it from an identically-named genus. Hemihomonyms are the case this project exists for —
`prunella` is the spec's own acceptance check — so `rank:map` failing one is a real mark against
the current default, and it is [tracked in future-work](future-work.md) rather than explained
away. Neither objective's failure is addressed by anything in the current feature set; both look
like they need either a feature that encodes kingdom disagreement as a hard signal, or a
preprocessing step that refuses cross-kingdom candidates outright.

---

## 6. Why `rank:map` is the pick

*Rewritten at milestone 15. This section previously picked `binary:logistic`, on a margin that
turned out to be about twice the noise floor and on one argument that no longer reproduces.*

On the promoted champion (§10):

| | `rank:map` | `binary:logistic` |
|---|---|---|
| Gold top-1 | 98.26% | 98.26% |
| Gold MRR | **0.9913** | 0.9906 |
| Gold Brier | **0.0083** | 0.0100 |
| Gold top-1, non-trivial items only | 97.7% | **98.3%** |
| Clears the 99.5% auto-accept bar | yes (9 rows) | yes (6 rows) |
| **Reject threshold re-applied to gold: true matches hidden** | **5 of 263** | 98 of 263 |
| OOF top-1 | 98.98% | **99.08%** |

**Gold top-1 is exactly tied** — both miss 4 of the 230 answerable items, three of them the same
items. The pre-registered rule breaks that tie on Brier, which selects `rank:map`. That
difference is 0.0017 against a noise floor of roughly 0.0043 (§10), so on its own it is not
decisive, and it is worth saying so rather than dressing it up.

What actually separates them is §2: **`rank:map`'s reject threshold survives the population change
and `binary:logistic`'s does not** — 5 hidden true matches against 98. For a system whose purpose
is to shrink a review queue, that is the difference between a usable band and an unusable one, and
it is not a marginal effect.

Two arguments from the previous version of this section have been retired:

- *"the only variant clearing the 99.5% auto-accept bar"* — on the current models both clear it,
  and even v1's recompute in the current environment has `rank` clearing at 5 rows. That was an
  artifact of the pre-`uv.lock` environment (§10).
- *"the gap widens on the non-trivial subset"* — it now runs the other way (98.3% against 97.7%),
  by one item on 206. Also inside the floor.

`binary:logistic` remains registered, versioned and one alias away; the two are close enough that
a larger gold set could reasonably reverse this again. That is the honest state of it.

`rank:map` was chosen over the spec's literal `rank:pairwise` because XGBoost's current
documentation recommends it for binary-relevance labels with enough data, which is exactly this
problem.

---

## 7. What the model is actually using

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/shap-summary-dark.png">
  <img alt="SHAP beeswarm over a 5,000-row sample." src="img/shap-summary-light.png">
</picture>

`name_exact_raw` dominates, followed by `jaro_winkler_full` and `sim_margin_to_runner_up` (how
much better this candidate is than the runner-up in its own group). That ordering is what it
should be, and the group-context feature earning third place is the interesting part: the model
learned that a candidate's plausibility depends on what it is competing against, not only on its
own similarity.

Three individual cases, worked through in the notebook:

- **`Prunella`, the bird** — `name_exact_raw = 1` pushes hard toward a match, and
  `kingdom_match = 0` with `shared_ancestor_depth = 0` push it decisively back. Calibrated
  probability 0.025, correctly rejected for the right reasons. This is milestone 1's acceptance
  case and the whole reason the taxonomic features exist.
- **A confidently wrong row** — raw score 0.982, actually wrong, with strong string similarity
  and nothing in the feature set flagging it. The §1 plateau, seen one row at a time.
- **A correct low-confidence row** — the true match, dragged to calibrated probability 0.039 by
  weak string similarity and a rank disagreement. A genuinely hard case, not a bug.

---

## 8. Negatives come from the deployment distribution

Negatives are the other candidates that survive candidate generation for the same Wikidata item,
not taxa sampled at random from the 1.4M-row index. This is the difference between a metric that
means something and one that does not.

A random negative is trivially separable: different genus, different family, different kingdom,
no string overlap. A model trained against those learns "do the names look alike" and reports a
spectacular AUC. The negatives here are *Prunella* the bird against *Prunella* the mint —
identical strings, differing only in ancestry — because those are the pairs that actually reach
a human reviewer. Every number in this repo is measured against that harder distribution, which
is why the baseline scores 21.3% on the gold set rather than something respectable.

The same reasoning drives spec §3's synthetic abstention dropout: 15% of items have their true
label hidden, so the model has to learn that "none of these" is a valid answer. Without it, a
model trained only on items that have an answer will confidently invent one for items that do
not.

## 9. Rebuilding the features in SQL: what moved, and why

> **Measured at v0.3.0, and kept as the milestone-14 record.** Milestone 15 then *closed* every
> gap below: `sim_rank_in_group` and the ancestor tie-break got explicit rules on the pandas side
> matching the SQL ones, and `parent_name_jw` moved to a rapidfuzz UDF. The two paths now agree on
> **52 of 52 columns** and on row order, and `make parity` prints "Columns that differ: None". The
> section is left as it was written because the causes it identifies are the reason the alignment
> was possible, and because a parity report that has been retro-fitted to its own fix records
> nothing. §10 covers what changed and why it had to.

Spec §7 milestone 14 moves feature construction from pandas into dbt-core over DuckDB. Its
acceptance check is not "the numbers match" — `docs/platform-design.md` §2.2 accepts drift
deliberately — but that every column which differs has a written cause. This is that comparison,
over the full 590,671-row frame. Reproduce it with `make features-sql && make parity`.

**46 of 52 columns are identical**, including the labels, the fold assignment, the family
grouping key, all ten strategy one-hots, both name-collision counts and
`sim_margin_to_runner_up`. Six differ.

| column | rows differing | share | max abs delta | cause |
|---|---:|---:|---:|---|
| `sim_rank_in_group` | 225,911 | 38.25% | 19 | tie-break rule |
| `shared_ancestor_depth` | 8,321 | 1.41% | 2 | ancestor tie-break (sum of the three below) |
| `order_match` | 7,184 | 1.22% | — | ancestor tie-break |
| `parent_name_jw` | 3,976 | 0.67% | 0.786 | bytes vs code points |
| `family_match` | 1,185 | 0.20% | — | ancestor tie-break |
| `kingdom_match` | 9 | 0.00% | — | ancestor tie-break |

### The 38% column is the least interesting one

`sim_rank_in_group` ranks a candidate against the others for the same item. pandas uses
`.rank(method="first")`, so **ties break on row order** — and ties are the common case, not the
exception, because every exact-match candidate scores `similarity = 1.0`. The row order it falls
back on comes out of `candidates.py`'s `imap_unordered` pool, so it is not stable across
regenerations on the pandas side either. SQL has no implicit row order, so the rule is now
explicit: highest similarity first, then lowest `inat_taxon_id`.

Every one of the 225,911 differing rows sits inside a group of equally-similar candidates — checked,
not assumed — and `sim_margin_to_runner_up`, which depends on the values rather than their order,
is bit-identical. 6,055 of 58,842 groups get a different rank-1 candidate out of it, but in both
implementations that candidate was picked arbitrarily from a set of identical scores.

This one was not in the audit that preceded the migration. It is now `platform-design.md` §4.3's
fourth entry, and it is the largest of the four.

### The taxonomic columns move only where the taxonomy is genuinely ambiguous

`kingdom_match` / `family_match` / `order_match` compare the two sides' ancestor *name* at that
rank. A transitive P171 chain can hold more than one ancestor at the same rank — 10,102 of 58,842
items do — and pandas resolved that by taking whichever row came first in the parquet. SQL orders
by lowest QID number instead: the older, more established Wikidata item.

**100% of the differing rows fall inside those 10,102 items**, on all three columns. Not a single
row moved outside the ambiguous population, which is what makes this a tie-break change rather
than a bug. 1,277 items are affected in total, and `shared_ancestor_depth` — their sum — moves by
+0.13 on average, so the SQL rule agrees with iNaturalist slightly more often than the pandas one
did. That is not an argument for it; it is a coincidence worth recording so it is not mistaken for
one later.

### DuckDB counts bytes, rapidfuzz counts code points

The substitution `platform-design.md` §2.2 expected to be the main source of drift turned out to
be almost free. On ASCII input the two agree: `levenshtein` exactly, and `jaro_winkler` to a
maximum absolute difference of **5.55e-17** — one unit in the last place of a 64-bit float, on
every one of 590,671 rows. Three of the four string-similarity features therefore agree to
floating-point noise and nothing more.

The fourth does not, and the reason is worth knowing before reaching for these functions again:

```
jaro_winkler_similarity('abc', 'ab×c')  →  DuckDB 0.689   rapidfuzz 0.933
levenshtein('Müller', 'Muller')         →  DuckDB 2       rapidfuzz 1
```

DuckDB's string-similarity functions operate on **UTF-8 bytes**; rapidfuzz operates on code
points. A two-byte character counts twice, and both the edit distance and the length that
normalises it come out wrong for the comparison being made.

It reaches exactly one feature. Every *normalised* name is ASCII by construction — `normalize.py`'s
genus and epithet patterns are `[A-Za-z-]` — so the only feature computed on raw strings,
`parent_name_jw`, is the only one exposed. All 3,976 disagreements involve a non-ASCII raw name,
overwhelmingly the hybrid marker `×`. The SQL is left as it is, and the deltas measured, rather
than working around it: this is precisely the deliberate substitution §2.2 signed up for, and
milestone 15's retrain is where its cost gets priced.

### What did *not* move, and why that was a choice

`label`, `no_answer_reason`, `family_key` and `fold` are identical. They did not have to be:
§4.3.2 planned to accept a hash-modulo synthetic dropout, which would have relabelled a different
15% of groups and moved every downstream metric for a reason unrelated to the migration. Keeping
the real seeded function (as a dbt Python model) is what leaves the six columns above legible.
The same reasoning kept `normalize.py` as a DuckDB UDF rather than a `regexp_extract`
transcription — ten features derive from that parse, and the ten agree exactly.

### One pre-existing bug, faithfully reproduced

Porting the code is a good way to read it. The ten `strategy_*` one-hots are built with
`strategies.str.contains(tag, regex=False)`, and three tags are substrings of others — `exact` of
`synonym_exact` and `basionym_exact`, and the same for `genus_epithet_fuzzy` and
`epithet_genus_fuzzy`. `candidates.py`'s own comment asserts the tags "are chosen not to be
substrings of each other", which is not true. 735 rows are marked `strategy_exact` on the strength
of a synonym or basionym match; only 42 of them were literally tagged `exact`.

The SQL reproduces it, deliberately. The frozen models trained on this behaviour, and changing it
in the milestone whose entire job is to measure drift would have made every number above
uninterpretable. It is in [`future-work.md`](future-work.md) as a one-line fix to take with
milestone 15's retrain.

---

## 10. Releasing the freeze: five model versions, and what each change was worth

Milestone 15 replaced the prose freeze with an MLflow registry and then released it on purpose.
The retrain is a **ladder** — one registered version per change — rather than one combined
retrain, so that every delta is attributable to the thing that caused it.
[`future-work.md`](future-work.md) asked for exactly this for the monotone-constraint change: *"a
deliberate, fully-rescored comparison … rather than a patch"*.

Each rung is one commit and one `run_ladder.py --rung vN` at that commit. Gold set, n=263:

| metric | v1 | v2 | v3 | v4 | v5 |
|---|---:|---:|---:|---:|---:|
| gold top-1 `binary` | 0.9957 | 0.9957 | 0.9826 | **0.9870** | 0.9957 |
| gold top-1 `rank` | 0.9913 | 0.9957 | 0.9870 | **0.9870** | 0.9913 |
| gold MRR `binary` | 0.9978 | 0.9978 | 0.9906 | **0.9928** | 0.9978 |
| gold MRR `rank` | 0.9957 | 0.9978 | 0.9935 | **0.9935** | 0.9957 |
| gold Brier `binary` | 0.0118 | 0.0128 | 0.0095 | **0.0100** | 0.0095 |
| gold Brier `rank` | 0.0221 | 0.0332 | 0.0078 | **0.0083** | 0.0080 |
| gold band precision | 0.9762 | 0.9756 | 0.9833 | **0.9827** | 0.9781 |
| gold band n | 168 | 164 | 180 | **173** | 183 |
| OOF top-1 `binary` | 0.9913 | 0.9912 | 0.9907 | **0.9908** | 0.9902 |

All five rungs are re-scored against the **corrected** gold set — the per-miss review that closes
this milestone found `Q14908802` mislabelled (§5), and a label correction changes the measuring
instrument for every rung, not just the champion's. Rescoring used the models in
`data/ladder/*/`; no rung was retrained.

| rung | change |
|---|---|
| v1 | the frozen milestone 6/7 binaries, back-filled |
| v2 | deterministic feature row order — **no feature definition changed** |
| v3 | the dbt feature table becomes canonical, after aligning three tie-breaks |
| v4 | the `strategy_*` one-hot substring fix — **the champion** |
| v5 | monotone constraints extended — ineligible, see below |

### v2 measures the noise floor, and it is not small

v2 changes no feature definition at all. `features.parquet` was simply not written in a
deterministic order — `candidates.parquet` comes out of an `imap_unordered` pool, and
`TREE_PARAMS`'s `subsample=0.8` selects rows by *position*, so rebuilding the table from
byte-identical inputs trained a different model. A fifth order-dependency after §9's four.

Its delta is therefore what a pure reshuffle is worth: **one full gold item** of `rank:map`'s
top-1 (0.9913 → 0.9957) and half a point of band precision, with the band's membership moving by
four rows (168 → 164). `binary:logistic` did not move at all, and OOF is stable to ~0.05pp because
590,671 rows average the reshuffle out. It is the 263-item gold set where this bites.

Two published claims are qualified by it, and this is the main reason the rung was worth running:

- §6 picked `binary:logistic` over `rank:map` on what the README calls "a two-item difference".
  The floor is about one item, so that margin was roughly twice the noise, not comfortably above
  it.
- §1's gold band precision moves on row order alone, and so does which rows are in the band.

**Anything on this gold set smaller than about two items should be read as noise.**

### The frozen models were not reproducible, for two independent reasons

Both measured while back-filling v1, and worth separating because they are usually conflated:

1. **Environment drift.** `data/models/*` are dated 2026-08-23; the venv was rebuilt 2026-08-29
   for milestones 13/14. Feeding the *same* `features.parquet` through the current environment
   moves every one of 590,671 raw scores and `binary_avg_best_iteration` 882 → 841. It is **not**
   thread count — `n_jobs` ∈ {1,4,8,20} give bit-identical fits, which contradicts
   `docker/Dockerfile`'s stated reason for pinning `OMP_NUM_THREADS=4`.
2. **Row order**, as above.

Aggregate metrics survive both (0.9913 against the published 99.1%), which is why this hid for so
long: only row-level scores and `best_iteration` move. v1's *gold* metrics are reproducible,
because they come from scoring the committed binaries, and CI proves that on every push.

### The pre-registered rule, and the two places it was uncomfortable

Written before any rung had run: eligibility gate (OOF top-1 must not regress more than 0.1pp
against v1), then gold top-1 with a ±2-item practical-equivalence band, then gold band precision,
then gold Brier, then the lower-numbered version.

**v5 ties for the best gold top-1 of any rung (0.9957, with v1 and v2) and is ineligible.** It
regresses OOF top-1 by 0.106pp against a gate of 0.100pp — it misses by 0.006pp. Recording that
instead of moving the threshold is the entire reason the threshold was written down first. Note
also that v5 does **not** fix `Q20668495`, the hemihomonym `rank:map` uniquely misses (§5), even
though that miss has exactly the shape §4's constraint argument describes — checked directly,
and it weakens rather than strengthens the case for the rung. Its *mechanism* fix was kept, because that part was a real bug rather than a
tuning choice: §4 and `future-work.md` both call the change "a one-line change to `MONOTONE_UP`",
and it never could have been. `sim_rank_in_group` is built with `rank(ascending=False)`, so rank 1
is the **best** candidate and the feature is inversely related to quality; putting it in
`MONOTONE_UP` would have constrained it backwards, and `monotone_constraints_tuple()` could emit
only `1` or `0`, so `-1` was not expressible at all. Both are fixed; only the constraint set is
unadopted, and `MONOTONE_DOWN` is empty.

**On the labels as they stood, the rule then selected v3, which was not a coherent answer.** The
rungs are cumulative code states, not alternatives, so promoting v3 would have meant reverting
v4's `strategy_*` correctness fix — on the strength of a 0.0005 Brier difference and one gold
item, both inside the noise floor v2 had just measured. **v4 was promoted instead: the latest
eligible rung.** That was a documented deviation from the rule as written, on the reasoning that a
correctness fix is not subject to a metrics vote while a tuning change is.

**The label correction then re-ran the rule, and v4 won it outright.** Correcting `Q14908802`
fixes the measuring instrument rather than moving a goalpost, so the rule was re-applied rather
than left standing on data known to be wrong. On the corrected set the primary criterion favours
v1 and v2 (0.9957), v4 sits two items back and therefore inside the ±2-item band, v3 sits three
items back and drops out, band precision leaves v1/v2/v4 tied, and **gold Brier picks v4** —
0.0100 against 0.0118 and 0.0128 on `binary`, and decisively on `rank`. The deviation above turned
out not to be load-bearing.

### The objective choice flipped, and not on the metric that looks decisive

§6 picked `binary:logistic`. On the promoted champion the two are **exactly tied** on gold top-1
(0.9870 — three misses each of 230 answerable items, two of them the same items), and `rank:map`
wins on gold Brier (0.0083 against 0.0100) and gold MRR (0.9935 against 0.9928). Applying the
rule's tie-break literally makes `rank:map` the reported default.

That tie-break is a 0.0017 Brier difference against a noise floor four times larger, so it should
not be read as decisive on its own. Two other things carry more weight:

- **§6's stated reason no longer holds.** It picked `binary` partly as "the only variant clearing
  the 99.5% auto-accept bar at all". On the champion both clear it — `binary` with 6 rows,
  `rank` with 9 — and even v1's current-environment recompute has `rank` clearing at 5. That
  claim was an artifact of the pre-`uv.lock` environment and does not reproduce.
- **The reject threshold transfers for `rank:map` and not for `binary:logistic`**, which is a
  large operational difference rather than a marginal one. See §2, now rewritten.

### What this milestone did not do

It did not make the model better. On the corrected gold set the champion ranks 98.70% against the
frozen models' 99.13% for `rank:map` — **one item worse**, inside the noise floor v2 measured, and
two items worse on `binary`. A retrain that costs an item is the honest outcome here and it is
reported as one.

What it produced instead: a registry where every published number resolves to a logged metric on a
named run; a feature pipeline that reproduces when rebuilt, and two feature paths that now agree
on all 52 columns rather than 46; three real bugs fixed (the `strategy_*` one-hots, monotone
constraints that could not express a decreasing feature, and `make gold` reaching the network
while two files claimed it did not); a measured noise floor for every future comparison on this
gold set; the discovery that one objective has a usable reject threshold and the other does not;
and — through the per-miss review the milestone ends with — a third mislabelled gold item found
and corrected.
