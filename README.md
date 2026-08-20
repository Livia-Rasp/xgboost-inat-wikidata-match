# xgboost-inat-wikidata-match

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides
whether a Wikidata taxon item and a candidate iNaturalist taxon refer to the same taxon. Built
on the data already indexed by
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker), with the goal of
shrinking the manual review queue its `npm run links` command produces.

Full specification: [`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md).

## Status

Milestones 1–2 of the spec's §7 list are done.

- **Ingest** (`src/normalize.py`, `src/candidates.py`): name-normalisation rules from spec §1,
  and a cached normalised-name + FTS5 trigram lookup table built from the local iNat taxa index,
  read-only.
- **Wikidata pull** (`src/wikidata.py`): batched SPARQL, `LIMIT`-capped to 60,000 taxa with
  P3151 set (of 856,040 that actually have it — see Commands below), cached to parquet.

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

**Ingest check (milestone 1).** Reads `~/.cache/wikidata-inat-checker/taxa.db` read-only,
builds a cache at `data/lookup.sqlite` (gitignored — first run ~20s over 1.4M rows, cached
after), and looks up `prunella`, the spec's acceptance check: a genus name shared by a bird
family (Prunellidae) and a mint family (Lamiaceae) that only differ by ancestry. Run from the
repo root; needs no dependencies beyond the standard library, so `python3` (no venv) works too.

```
python3 -m src.candidates
```

Expected output:

```
2 match(es) for 'prunella':
  {'taxon_id': '13982', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/1/2/355675/3/7251/71358'}
  {'taxon_id': '52765', 'name': 'Prunella', 'rank': 'genus', 'ancestry': '48460/47126/211194/47125/47124/48151/48623/520502/918917/919181'}
```

**Wikidata pull (milestone 2).** Batched SPARQL against `https://query.wikidata.org/sparql` for
taxa carrying P3151, cached to `data/wikidata_taxa.parquet` (+ a sidecar manifest that the
cache-hit check compares against, gitignored). Needs the venv (`pandas`, `pyarrow`, `requests`).
First run takes a few minutes and makes ~30 batched network requests; reruns are a cache hit.

```
.venv/bin/python -m src.wikidata
```

Expected output (first run — a fresh pull; reruns say `cache hit, no network` and return in
under a second):

```
58,064 Wikidata taxa with P3151 (fresh pull)
        qid inat_id                      name rank_qid parent_qid   parent_name iucn_qid  sitelinks  statements  has_commons_cat  p3151_has_reference
0   Q557493   18161    Melanerpes uropygialis    Q7432    Q131901    Melanerpes  Q211005         31          74             True                False
1   Q569535   18205      Melanerpes carolinus    Q7432    Q131901    Melanerpes  Q211005         34          94             True                False
2  Q1263378    1371  Odontophorus leucolaemus    Q7432   Q1080907  Odontophorus  Q211005         22          60             True                False
```

No other commands exist yet — this section grows as the remaining milestones (§7) land.
