-- Transitive P171 pairs, with the rank QID resolved to a comparable rank name. Rows whose
-- ancestor has no mapped rank are kept: int_ancestor_by_rank filters, and dropping them here
-- would hide how much of the chain is unmapped.
select
    a.qid                as wikidata_qid,
    a.ancestor_qid,
    a.ancestor_name,
    a.ancestor_rank_qid,
    r.rank_name          as ancestor_rank_name
from {{ source('caches', 'wikidata_ancestors') }} as a
left join {{ ref('stg_rank_names') }} as r
    on a.ancestor_rank_qid = r.rank_qid
