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

## Test the extended monotone constraints properly

[`findings.md`](findings.md) §4 has a concrete case where leaving `sim_rank_in_group` and
`family_match` unconstrained lets a strictly worse candidate win. The fix is a one-line change to
`MONOTONE_UP` and it does resolve that case, but adopting it means retraining, which breaks the
model freeze every number in this repo is quoted against. Worth doing as a deliberate,
fully-rescored comparison — new models, all metrics regenerated, both variants — rather than as a
patch.

Spec milestone 15 releases that freeze on purpose and retrains, which is the natural moment to
fold this in: the rescoring is happening anyway, and MLflow makes the before/after a comparison
between two registered versions rather than a claim. The same applies to the ligature note below.

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

## Smaller things

- **Rank restriction as an ablation.** Restricting to species-rank items would remove a class of
  genuine ambiguity (a species complex sharing its name with its representative species). That is
  a modelling-stage ablation to measure, not a data-cleaning step to apply — the rank-trivial
  breakdown in `evaluate.py` already isolates the affected bucket.
- **`log1p(inat_observation_count)` as a feature.** Spec marks it optional and it needs the 12.7
  GB observations dump; the baseline already sources a scoped version of this signal from the API
  for tie-breaking, so the plumbing exists if it turns out to be worth it.
- **Ligatures in normalisation.** NFKD decomposes accents but not `æ`/`œ`/`ß`, so a name
  containing one either loses its epithet (ligature in the epithet) or parses empty (ligature in
  the genus). There are zero such names in the current 1.4M-row iNat index, and changing
  normalisation would invalidate every cached feature the frozen models trained against, so the
  behaviour is pinned by a test rather than fixed. Worth doing whenever the models are next
  retrained.
- **Stale P3151 references.** 12.85% of P3151 links point to iNat taxon IDs that no longer exist
  as active taxa. This project treats them as unresolvable and reports them separately. They are
  also a fixable data-quality problem in Wikidata in their own right.
