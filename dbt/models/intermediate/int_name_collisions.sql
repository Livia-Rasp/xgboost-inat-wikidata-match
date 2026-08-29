-- How many taxa share a name string, on each side. These are the two homonym-pressure features:
-- a candidate whose name is one of 40 identical strings in the iNat index is weaker evidence
-- than one whose name is unique.
--
-- Counted over the *whole* 1.4M-row index and the whole 58,874-item Wikidata pull, not over the
-- candidate set, and on the **raw** name string rather than the normalised one — matching
-- features.py:204-207. Normalising here would merge "Prunella L." into "Prunella" and change
-- what the feature measures.
--
-- One model, two grains, kept apart by `side`: the alternative is two near-identical models, and
-- fct_features joins each side on its own key either way.
select
    'inat'      as side,
    inat_name   as name,
    count(*)    as n_taxa_same_name
from {{ ref('stg_inat_taxa') }}
group by 1, 2

union all

select
    'wikidata'      as side,
    wikidata_name   as name,
    count(*)        as n_taxa_same_name
from {{ ref('stg_wd_taxa') }}
group by 1, 2
