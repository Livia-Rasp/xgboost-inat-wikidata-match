# xgboost-inat-wikidata-match

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides
whether a Wikidata taxon item and a candidate iNaturalist taxon refer to the same taxon. Built
on the data already indexed by
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker), with the goal of
shrinking the manual review queue its `npm run links` command produces.

Full specification: [`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md).

## Status

Milestone 1 of the spec's §7 list (**ingest**) is done. `src/normalize.py` implements the
name-normalisation rules from spec §1; `src/candidates.py` builds a cached normalised-name +
FTS5 trigram lookup table from the local iNat taxa index, read-only. No other milestone is
implemented yet.

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

```
pip install -e ".[dev]"
```

**Ingest check (milestone 1).** Reads `~/.cache/wikidata-inat-checker/taxa.db` read-only,
builds a cache at `data/lookup.sqlite` (gitignored — first run ~20s over 1.4M rows, cached
after), and looks up `prunella`, the spec's acceptance check: a genus name shared by a bird
family (Prunellidae) and a mint family (Lamiaceae) that only differ by ancestry. Run from the
repo root; needs no dependencies beyond the standard library.

```
python3 -m src.candidates
```

Expected output:

```
2 match(es) for 'prunella':
  {'taxon_id': '13982', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/1/2/355675/3/7251/71358'}
  {'taxon_id': '52765', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/47126/211194/47125/47124/48151/48623/520502/918917/919181'}
```

No other commands exist yet — this section grows as the remaining milestones (§7) land.
