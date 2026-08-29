-- The grain: one row per (Wikidata item, iNat candidate). candidates.py caps at K=20 per item and
-- dedupes on taxon_id, so a duplicate here means one of fct_features' eight joins fanned out.
--
-- Worth a test rather than trust, because the failure is silent: a fanned-out join does not error,
-- it just reweights the training population toward whichever rows duplicated, and every metric
-- downstream still computes.
select
    wikidata_qid,
    inat_taxon_id,
    count(*) as n_rows
from {{ ref('fct_features') }}
group by 1, 2
having count(*) > 1
