# Findings

What the numbers turned out to mean, and how each conclusion was checked. Every figure here is
reproducible with the commands in [`../README.md`](../README.md); the plots come from
`build_figures.py`.

---

## 1. The model was overconfident, and the reason was the labels

Isotonic calibration on the out-of-fold scores surfaced a gap that a plain accuracy number hides
completely. 50,296 candidate rows score ≥0.95 on the raw `binary:logistic` probability, but only
**83.9%** of them are actually correct. Calibration does the right thing and compresses that
whole cluster down to ~84%, which is why the strict 99.5%-precision auto-accept band ends up
covering **10 rows out of 590,671**.

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

**Result:** on the hand-labelled gold set, the same raw-score band reads **98.2% precision**
(n=167) against 83.9% on the P3151 population. The ceiling was in the labels, not the model.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/calibration-dark.png">
  <img alt="Reliability diagram of raw versus isotonic-calibrated scores." src="img/calibration-light.png">
</picture>

This does not mean P3151 is 14 points wrong. The gold set is a different, harder population
(ambiguous items by construction) and 167 rows is a small sample. It does mean the
overconfidence measured against P3151 cannot be read as a model deficiency, which is what the
raw number would otherwise imply.

---

## 2. The reject threshold does not survive the population change

Both thresholds are chosen on OOF data at 99.5% precision. On that population the reject side
does almost all of the useful work: rows below 0.82 calibrated probability are negative
**99.61%** of the time, and that covers **91.6%** of all candidate rows.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/threshold-bands-dark.png">
  <img alt="The reject, review and auto-accept bands drawn to scale." src="img/threshold-bands-light.png">
</picture>

Re-applying that same threshold to the gold set, unchanged, it drops to **95.7%** row-level
negative precision — and it would hide the true match for **107 of 263 items**. The auto-accept
threshold transfers no better: zero gold rows clear 0.87.

This is a real limitation, not a presentation problem, and it is the reason this project's
headline is stated as ranking quality rather than as automated queue clearance. The thresholds
were fitted on a population where most items have one obvious answer. The queue this system is
meant to shrink consists, by definition, of the items where that is not true. Anything derived
from a threshold has to be re-derived on the deployment population before it can be trusted.

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

| gold set, n=263 | ranked by calibrated prob | ranked by raw score |
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
aggregate. At n=263, `binary:logistic` misses 3 items and `rank:map` misses 5.
`src/evaluate.py --gold` prints them with their full feature breakdown.

| Item | Picked | Correct | Reading |
|---|---|---|---|
| `Q4694188` *Agrisius japonicus* | `1265680` | `1265680` | **Was a labelling error.** Recorded as `265680` — a dropped leading digit, landing on *Poecilopompilus algidus*, a spider wasp that was never among the offered candidates. Corrected. It was the sole reason the gold recall ceiling read 99.57% instead of 100%. |
| `Q121887868` *Acanthocephala* | `61446` (genus) | `151826` (phylum) | **Model gap.** The Wikidata item is a phylum and only the correct candidate matches that rank, yet the model ranked the identically-named genus first. The one miss in the trivial-by-rank bucket. |
| `Q25661362` *Auricula* (`rank:map` only) | `742765` (section) | `1269419` (genus) | **Model gap, same shape as above.** `binary:logistic` gets it right. |
| `Q16760098` *Utetheisa albilinea* | `1522832` | `1550468` | **iNat data issue.** Two iNat records for what appears to be the same taxon, differing only by an extra subgenus in the ancestry. Either answer is defensible; the labeller's note says as much. |
| `Q46674974` *Bursaria* | `83575` | `1051126` | **iNat data issue.** Two genus records with incomplete ancestry, so no taxonomic feature can separate them. |
| `Q106123971` *Acmella pusilla* (`rank:map` only) | `157976` | `1349465` | **Model gap specific to `rank:map`** — the unconstrained-interaction case in §4. |

Two of the three surviving `binary:logistic` misses are iNat-side duplicate records rather than
ranking failures. That is worth knowing before chasing the last point of accuracy: the remaining
headroom on this sample is mostly not in the model.

The per-miss review has now caught a labelling error on both runs it has been done on (an earlier
50-item run caught `Q21438872`, where the stated rank matched a subgenus but the true answer was
the nominotypical genus of the same name). It is cheap and it keeps paying.

---

## 6. Why `binary:logistic` is the pick

| | `binary:logistic` | `rank:map` |
|---|---|---|
| Gold top-1 | **98.7%** | 97.8% |
| Gold MRR | **0.993** | 0.989 |
| Gold Brier | **0.013** | 0.024 |
| Gold top-1, non-trivial items only | **98.9%** | 97.7% |
| Clears the 99.5% auto-accept bar | yes (10 rows) | no |
| OOF top-1 | 99.1% | 99.0% |

They are close on the OOF population, which is why milestone 6 deliberately declined to pick a
winner there and deferred to the gold set. On gold, `binary:logistic` leads on every metric, and
the gap widens on the non-trivial subset — the items where rank alone does not resolve the
answer, which is the part that actually tests judgment. Its calibrated probabilities are also
markedly better (Brier 0.013 vs 0.024), which matters for any future threshold work.

`rank:map` was chosen over the spec's literal `rank:pairwise` because XGBoost's current
documentation recommends it for binary-relevance labels with enough data, which is exactly this
problem. That substitution was worth making; the objective still lost.

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
is why the baseline scores 20.9% on the gold set rather than something respectable.

The same reasoning drives spec §3's synthetic abstention dropout: 15% of items have their true
label hidden, so the model has to learn that "none of these" is a valid answer. Without it, a
model trained only on items that have an answer will confidently invent one for items that do
not.

## 9. Rebuilding the features in SQL: what moved, and why

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
