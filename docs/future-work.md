# Future work

The study in the README is closed at its stated scope: 263 hand-labelled ambiguous items, two
objective variants compared, a winner picked. These are the things that would extend it, roughly
in the order they are worth doing.

## Write the resolved links back to Wikidata (spec §7 milestone 10)

Hand-labelling the gold set already resolved 230 ambiguous taxa that no automated match could.
Those links belong in Wikidata, not in a CSV. A script reading `gold/hard_cases.csv`'s confirmed
matches (`label == 1`) and emitting one `{qid}\tP3151\t"{inatId}"` line each would make that a
single [QuickStatements](https://quickstatements.toolforge.org/) batch instead of a per-row copy
button that is easy to lose track of.

The labelling page's copy button already works one row at a time, so this is a convenience and an
audit trail rather than a capability. It is listed first because it is the only item here with a
real-world effect outside this repo.

## Label the rest of the sample

`gold/labeling_filled.csv` holds 883 sampled ambiguous items, of which 263 are answered. Every
additional label tightens the same confidence intervals; nothing about the pipeline needs to
change. The two questions most likely to move are whether the `binary:logistic` / `rank:map` gap
holds up (it currently rests on 6 items' worth of difference), and whether the label-noise result
in [`findings.md`](findings.md) §1 stays near 98% as the sample grows.

## Re-derive the thresholds on the ambiguous population

[`findings.md`](findings.md) §2 is the most actionable negative result here: thresholds fitted on
the P3151 population do not transfer to the ambiguous queue. Both bands were chosen on OOF data
because that is the only population large enough to fit them on. Once the gold set is large
enough to fit rather than merely check a threshold, they should be re-derived there — the
deployment distribution and the fitting distribution would finally be the same one.

## Stop the model matching across kingdoms

The clearest open weakness, found in milestone 15's per-miss review ([`findings.md`](findings.md)
§5). `Q20668495` *Afrocrania* is a **hemihomonym**: Wikidata's is a plant genus (parent
*Cornaceae*), iNaturalist holds that plant as a **subgenus** and a different, animal *Afrocrania*
at **genus** rank. `rank_equal` therefore points at the animal while `kingdom_match`,
`family_match`, `order_match` and `shared_ancestor_depth` all point at the plant.
`binary:logistic` weighs the taxonomy and gets it right; `rank:map`, the current default, weighs
`rank_equal` and does not.

Matching a taxon to something in another kingdom is not a near-miss, it is a category error, and
hemihomonyms are the case this project exists for — `prunella` is the spec's own acceptance check
in §7 milestone 1. Two directions, not mutually exclusive:

- **A feature that encodes kingdom disagreement as a hard signal.** `kingdom_match` is currently
  one of three equal-weight boolean agreements folded into `shared_ancestor_depth`. A *confirmed
  kingdom mismatch* — both sides have a known kingdom and they differ — is categorically stronger
  evidence than "no ancestor matched", and the feature set cannot presently tell those apart.
- **Preprocessing that refuses cross-kingdom candidates outright.** Cheaper and blunter. It would
  need care: the gold set contains items whose Wikidata ancestry is incomplete, so "unknown
  kingdom" must not be treated as "different kingdom", and candidate generation would want to keep
  surfacing these so the recall ceiling stays measurable.

Note that v5's extended monotone constraints do **not** fix this case — checked directly during
the review — so it is a genuinely separate piece of work rather than something the parked rung
would pick up.

## Re-test the extended monotone constraints once the gold set is bigger

**Tested at milestone 15 as ladder rung v5, and not adopted.** It produced the best gold top-1
(0.9913) and best gold MRR (0.9957) of any rung, and it failed the pre-registered eligibility gate
by 0.006pp: the gate allowed an OOF top-1 regression of 0.100pp against v1 and v5 regressed
0.106pp. [`findings.md`](findings.md) §10 has the full comparison.

Two things to carry forward rather than repeat:

- The change was **never** "a one-line change to `MONOTONE_UP`", as this entry and §4 both used to
  say. `sim_rank_in_group` is built with `rank(ascending=False)`, so rank 1 is the *best*
  candidate; it needs a **decreasing** constraint, and `monotone_constraints_tuple()` could not
  emit `-1` at all. That mechanism is fixed and `MONOTONE_DOWN` exists; it is simply empty.
  Re-adopting is now genuinely a one-line change.
- v5's gold numbers were the best on the board. A gate missed by 0.006pp on a 263-item gold set is
  not strong evidence against it — but moving a pre-registered threshold after seeing what it
  excludes is worse than leaving a good change on the table. **The right way to revisit this is
  with more gold labels, not with a different gate.**

## Ligatures in normalisation

Deferred again at milestone 15. NFKD decomposes accents but not `æ`/`œ`/`ß`, so a name containing
one either loses its epithet or parses empty. There are still zero such names in the 1.4M-row iNat
index, so the change is provably a no-op on current data — which is exactly why it did not get a
ladder rung: it would have added a code change with no measurable effect to a ladder whose only
purpose was attributing measured differences, and it would have meant inverting
`test_ligatures_are_a_known_gap` to gain nothing. The behaviour stays pinned by that test.

## Close the loop back into the Node tool (spec §7 milestone 12)

Scoring the ambiguous queue where it actually lives, instead of alongside it.
`wikidata-inat-checker` has since moved that queue into a database and a webapp, and reserves a
`score` and `scoredBy` field — always null today — on every ambiguous candidate it records, so
the slot for this exists on their side already.

**Direction, once it happens: the checker calls a scoring service exposed by this repo, not this
repo writing rows into the checker's database.** That keeps the model's deployment surface where
the registry and the feature code are, and keeps `findings.db` owned by exactly one writer. Not
settled — the shape is worth a proper discussion before either side builds to it. Spec milestone
16 therefore reads the checker's findings and stops there.

Two things block the useful version regardless. The threshold work above: without thresholds that
hold on this population, the only honest output is a ranking, which the queue could equally well
just sort by. And on the checker's side, `POST /api/findings/:id/pick` overwrites the ambiguous
row in place, discarding the rejected candidates — so human decisions there yield positives but
no per-candidate negatives, and no audit trail of what was rejected.

## Read the checker's findings from dbt (spec §7 milestone 16)

`docs/platform-design.md` §5.2 planned a `stg_link_findings` staging model attaching the sibling
repo's `findings.db`. Milestone 14 left it out: nothing in the feature pipeline consumes it, and a
model that fails whenever the sibling repo is absent would break `dbt build` in the container,
which is exactly the property milestone 13 spent its effort on. Milestone 16's scoring DAG is what
actually needs those rows, and it can add the model when it does — read-only, per §2.4.

## Smaller things

- ~~**`strategy_exact` is true for `synonym_exact` and `basionym_exact` rows.**~~ **Fixed at
  milestone 15**, ladder rung v4, in both the pandas and SQL paths in one commit so parity held.
  693 rows lost a `strategy_exact` they should never have had, and `candidates.py`'s comment
  claiming the tags are not substrings of each other now says what is actually true. The
  correctness fix is also why v4 was promoted over v3 despite v3 winning the pre-registered
  tie-break — see [`findings.md`](findings.md) §10.
- **Rank restriction as an ablation.** Restricting to species-rank items would remove a class of
  genuine ambiguity (a species complex sharing its name with its representative species). That is
  a modelling-stage ablation to measure, not a data-cleaning step to apply — the rank-trivial
  breakdown in `evaluate.py` already isolates the affected bucket.
- **`log1p(inat_observation_count)` as a feature.** Spec marks it optional and it needs the 12.7
  GB observations dump; the baseline already sources a scoped version of this signal from the API
  for tie-breaking, so the plumbing exists if it turns out to be worth it.
  (Ligatures had its own entry here; it is now a section above, since milestone 15 considered it
  explicitly and deferred it for a stated reason rather than by omission.)
- **Stale P3151 references.** 12.85% of P3151 links point to iNat taxon IDs that no longer exist
  as active taxa. This project treats them as unresolvable and reports them separately. They are
  also a fixable data-quality problem in Wikidata in their own right.
