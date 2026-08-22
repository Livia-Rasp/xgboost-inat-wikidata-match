# Gold set — generation, labeling, and evaluation

Spec §6 point 6 / §7 milestone 7: hand-label ~200+ rows sampled from `links-ambiguous.html` and
use them as the real test set — the only evaluation not contaminated by bot-added P3151 labels.
This is the whole reproduction + labeling workflow.

## 1. Generate the source material (reproducible)

`links-ambiguous.html` comes from the sibling
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker) repo's own `links`
checker. "Ambiguous" there means a Wikidata taxon's scientific name matches **2+ active iNat
taxa exactly** — pure name-collision (hemihomonyms), the same problem this whole project exists
to resolve. Only ~0.57% of what the checker scans lands in that bucket (2,622/458,092, per its
own full-population survey — see `docs/links.md` in that repo), so getting 300+ ambiguous rows
needs a large scan, not the default `--limit 200`.

```sh
cd ~/repos/wikidata-inat-checker
rm -f cache/cache-links.json    # clears 200 stale QIDs from a prior small run, so nothing gets skipped
npm run links -- --limit 80000 --ambiguous-only
```

Read-only against both Wikidata and iNaturalist (SPARQL `SELECT` queries only — verified by
tracing every network call in `checkLinks.js`'s import graph, no Wikidata edit action anywhere).
Writes only to that repo's own `output/` and `cache/`. Expect on the order of ~450 ambiguous
items (0.57% × 80,000) — comfortably above the 300+ target with room to skip.

`--ambiguous-only` is a flag added to that repo specifically for this workflow: it skips
`output/links.html`'s P3151 cross-check and its ancestor-chain fetch for every clean match
(tens of thousands of items at this `--limit`, by far the slowest and most WDQS-load-sensitive
part of a run) and writes only `output/links-ambiguous.html`, which is all this project needs.
Without it, this same scan repeatedly failed against WDQS instability that surfaced as this
project's own milestone 4 (`ANCESTOR_MIN_COVERAGE`) had already flagged as a known WDQS
behavior — see `CLAUDE.md`'s milestone 7 notes for the full failure/fix history. With the flag,
a real `--limit 80000` run completed in 5m36s and found 476 ambiguous items.

**Milestone 8 fix: the scan order is now shuffled before `--limit` is applied.** The first
`--limit 80000 --ambiguous-only` run above only ever reached names "Abietinella" through
"Cattleya" — `allNames()` (`lib/getInatTaxaDb.js` in the sibling repo) runs
`SELECT DISTINCT name FROM taxa` with no `ORDER BY`, but SQLite's `DISTINCT` implementation
happens to produce alphabetically-sorted output as a side effect, so the entire 80,000-name
budget was spent on the alphabet's early names before the scan ever reached the rest. Fixed
upstream: `checkLinks.js` now shuffles the name list (seeded, reproducible — `--seed <n>`,
default `42`) before applying `--limit`, so a rerun samples across the full alphabet instead of
stopping partway through it. See that repo's `docs/links.md` for details.

Re-running the scan command above (after `rm -f cache/cache-links.json`) will find a fresh,
alphabetically-diverse batch of ambiguous items. `build_gold_labeling_kit.py` (§2 below) is
merge-aware, so this is safe to do without losing any existing labels.

## 2. Build the labeling kit

```sh
.venv/bin/python build_gold_labeling_kit.py
```

Parses `~/repos/wikidata-inat-checker/output/links-ambiguous.html`. **Merge-aware, no target
size, appends in place**: it reads `gold/labeling_filled.csv` if it exists, excludes every QID
already in there (answered or deliberately left blank — nothing already shown to you gets
resampled), and appends one blank-answer row per *remaining* ambiguous item straight onto
`gold/labeling_filled.csv` itself (creating it, with header, on the very first run) — there's no
cap to sample down from and no separate template file to "save as" afterward. If that's more new
rows than you want to work through in one go, leaving them blank is the intended way to bound
your own effort. It also writes a trimmed review HTML, scoped to just the new items — the exact
same reviewing page as the original tool (checkboxes, WD/iNat links, taxonomy tree-pair
comparison with green/red rank-agreement highlighting, click-to-copy QuickStatements). This
tree-pair comparison is the right signal for this specific judgment — it's a taxonomic
name-collision question, not a visual species-ID one, so there are no photos and none are needed.
The first run writes `links-ambiguous-sample.html`; **a later run never overwrites an existing
sample file** (you might still be working through it) — it writes
`links-ambiguous-sample-2.html`, `-3.html`, and so on instead. If there are no new items (e.g.
rerunning against the same source HTML), nothing is appended and no sample HTML is written.

## 3. Your labeling workflow

1. Open the newest `gold/links-ambiguous-sample*.html` in a browser and `gold/labeling_filled.csv`
   in a spreadsheet app, side by side. (Older `-N.html` files from previous batches are still
   there if you're finishing up an earlier round — nothing gets overwritten.)
2. For each new row in the CSV (matched to the HTML by `wikidata_qid`), use the HTML's tree-pair
   comparison to judge which candidate (if any) is the right match, then in the CSV:
   - Type the correct `inat_taxon_id` (copy it from `candidate_inat_ids` or the HTML) into
     `correct_inat_taxon_id`.
   - If you've checked and **none** of the candidates are correct, type `NONE`.
   - **Leave the row blank to skip it** — there's no target row count to hit, so skip anything you
     can't decide and stop whenever you've got 300+ answered.
   - `notes` is optional — a short reason is useful later for the error-taxonomy writeup, but
     don't let it slow you down.
3. Ticking a row's checkbox in the HTML is just for your own progress-tracking (persists via the
   browser's local storage, same as the original tool) — it isn't read back into
   `gold/hard_cases.csv` or anything else in this repo.
4. The QuickStatements cell **is** meant to be used, though — click to copy `{qid} P3151
   "{inatId}"` for whichever candidate you resolve as correct, and paste it into
   [QuickStatements](https://quickstatements.toolforge.org/) to actually add that link to
   Wikidata. That's this whole project's real end goal, not a side effect of labeling: these are
   exactly the taxa an automated match can't confidently resolve, so hand-resolving them here and
   submitting the link isn't wasted effort spent only on an evaluation set — it's the deliverable.
5. Just save `gold/labeling_filled.csv` directly as you go — it's the one file, edited in place,
   no "save as" step. You can stop and resume any time; nothing needs to happen in one sitting.

**On writing back to Wikidata.** Every automated step in this whole project (every SPARQL pull in
`wikidata.py`, and `npm run links` itself) is strictly read-only — verified by tracing every
network call. Submitting QuickStatements from your labeling is the one deliberate exception: a
manual, human-initiated write, done outside any script here. It has one consequence worth
knowing about for *future* runs of this same workflow: once a P3151 link lands on an item, that
item permanently leaves the "no P3151" population `wikidata-inat-checker` scans, so it can never
resurface as a gold-set candidate again (`checkLinks.js`'s whole purpose is finding items
*without* P3151). `gold/hard_cases.csv` itself is unaffected — it's a frozen, committed snapshot
of what you labeled, regardless of what happens in Wikidata afterward — but the *pool* of
ambiguous items available to sample from on a future run will shrink over time, partly *because*
this workflow is succeeding at its actual goal.

## 4. Turn your answers into the committed gold set + evaluate

Once `gold/labeling_filled.csv` exists:

```sh
.venv/bin/python build_gold_set.py
.venv/bin/python -m src.evaluate --gold
```

`build_gold_set.py` pulls fresh Wikidata attributes + ancestor chains for your labeled items
(they were never in `data/wikidata_taxa.parquet` — that pull only covers items that *already*
have P3151, and gold-set items are specifically ones that don't yet, by construction), generates
candidates against the same local iNat index every other milestone uses, cross-checks your
answers against those candidates, and writes the long-format **`gold/hard_cases.csv`** (spec §0,
committed to the repo) that `src/evaluate.py --gold` then scores the saved models and baseline
against. See `README.md`/`CLAUDE.md` for what gets reported. This works fine on a partial
`labeling_filled.csv` too — you don't have to wait until you're done labeling to run it.

**Review the misses, don't just read the accuracy number.** Every top-1 miss `--gold` reports is
worth pulling apart individually (candidate scores, `kingdom_match`/`family_match`, `rank_equal`,
`strategies`, raw vs. calibrated score) before trusting an aggregate number, especially at small
sample sizes where one row can swing the percentage — and before trusting your own first
impression of what a miss looks like: verify against ancestry/feature data rather than
pattern-matching, since a plausible-looking guess (e.g. "these look like duplicate taxon
records") can be wrong (a plant and a mollusc sharing a species epithet look similar to a
duplicate at a glance, but aren't). Categories that showed up in the first 50-item test run: a
genuinely close low-confidence call (not concerning), a labeling error worth fixing in
`gold/labeling_filled.csv` (a WD item whose stated rank matched a subgenus, but the true answer
was the nominotypical genus of the same name), and a real `rank:map`-specific ranking miss where
the raw score disagreed with what the taxonomic-agreement features said, masked by isotonic
calibration mapping both candidates into the same output bucket — evidence for the binary-vs-rank
comparison milestone 7 has to make, not something to dismiss. Do this after every `--gold` run,
not just the final one.
