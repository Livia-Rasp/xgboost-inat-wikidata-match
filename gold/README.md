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

**Known limitation: the current sample only covers names starting A-C.** `allNames()`
(`lib/getInatTaxaDb.js` in the sibling repo) runs `SELECT DISTINCT name FROM taxa` with no
`ORDER BY`, but SQLite's `DISTINCT` implementation happens to produce alphabetically-sorted
output as a side effect — so `checkLinks.js` scans names in that incidental order, and
`--limit 80000` caps collected candidates before ever reaching past the C's. Confirmed on the
476-item sample actually generated (Abietinella to Cattleya, nothing later). Worth fixing before
treating the gold set as representative across taxonomic diversity — raising `--limit` further,
randomizing the scan order, or bucketing separate scans by alphabet range are the options, not
yet decided. Not urgent for early labeling, but flag it before drawing conclusions that assume
alphabetic representativeness.

## 2. Build the labeling kit

```sh
.venv/bin/python build_gold_labeling_kit.py
```

Parses `~/repos/wikidata-inat-checker/output/links-ambiguous.html`, randomly samples up to 500
items (seeded — `RANDOM_SEED = 42` in `build_gold_labeling_kit.py`, so this is reproducible; a
real `--limit 80000` run found only 476 ambiguous items total, fewer than 500, so it samples all
of them), and writes two files into `gold/`:

- **`links-ambiguous-sample.html`** — the exact same reviewing page as the original tool
  (checkboxes, WD/iNat links, taxonomy tree-pair comparison with green/red rank-agreement
  highlighting, click-to-copy QuickStatements), trimmed to the sampled items (all 476, on the
  real run). This tree-pair comparison is the right signal for this specific judgment — it's a
  taxonomic name-collision question, not a visual species-ID one, so there are no photos and
  none are needed.
- **`labeling_template.csv`** — one row per sampled item (not one per candidate, to keep your
  side of this quick): `wikidata_qid, wikidata_name, wikidata_url, candidate_inat_ids,
  candidate_inat_names, correct_inat_taxon_id, notes`. The last two columns are blank for you.

## 3. Your labeling workflow

1. Open `gold/links-ambiguous-sample.html` in a browser and `gold/labeling_template.csv` in a
   spreadsheet app, side by side.
2. For each row in the CSV (matched to the HTML by `wikidata_qid`), use the HTML's tree-pair
   comparison to judge which candidate (if any) is the right match, then in the CSV:
   - Type the correct `inat_taxon_id` (copy it from `candidate_inat_ids` or the HTML) into
     `correct_inat_taxon_id`.
   - If you've checked and **none** of the candidates are correct, type `NONE`.
   - **Leave the row blank to skip it** — the sample has 500 rows so you have plenty of room to
     skip ones you can't decide and still land well past 300 answered.
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
5. Save your work as **`gold/labeling_filled.csv`** (same columns, just filled in) once you've
   got 300+ non-blank answers. You can stop and resume any time; nothing needs to happen in one
   sitting.

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
