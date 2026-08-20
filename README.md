# xgboost-inat-wikidata-match

A record-linkage classifier ([XGBoost](https://xgboost.readthedocs.io/en/stable/)) that decides
whether a Wikidata taxon item and a candidate iNaturalist taxon refer to the same taxon. Built
on the data already indexed by
[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker), with the goal of
shrinking the manual review queue its `npm run links` command produces.

Full specification: [`docs/inat-wikidata-match-spec.md`](docs/inat-wikidata-match-spec.md).

## Status

Repo scaffold is in place (`pyproject.toml`, `src/` module stubs, `gold/`, `notebooks/`); no
milestone from the spec's §7 list is implemented yet.

## Install / run

```
pip install -e ".[dev]"
```

No scripts have real logic yet — this section will carry the real commands as the spec's
milestones (§7) land.
