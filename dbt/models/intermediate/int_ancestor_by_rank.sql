-- The Wikidata item's ancestor *name* at kingdom, family and order — the three ranks
-- kingdom_match / family_match / order_match compare on.
--
-- Two rules carried over from _wd_ancestor_names_by_rank() (src/features.py:63-81):
--
-- 1. An item at (or above) a target rank counts as its own ancestor there, and that never gets
--    overwritten by a chain entry. That is what `source_priority` encodes.
-- 2. A transitive P171 chain really can contain two ancestors at the same rank. Pandas takes
--    whichever came first in the parquet's row order (dict.setdefault over an iteration).
--    platform-design §4.3.1 calls this out as a documented cause of moved numbers: SQL has no
--    implicit row order, so the tie-break becomes explicit here — lowest QID number wins, i.e.
--    the older, more established Wikidata item. Deterministic, and independent of file layout.
with self_as_ancestor as (
    select
        w.wikidata_qid,
        r.rank_name,
        w.wikidata_name as ancestor_name,
        0               as source_priority,
        0               as qid_number
    from {{ ref('stg_wd_taxa') }} as w
    join {{ ref('stg_rank_names') }} as r
        on w.rank_qid = r.rank_qid
    where r.rank_name in {{ comparison_ranks() }}
),

from_chain as (
    select
        wikidata_qid,
        ancestor_rank_name as rank_name,
        ancestor_name,
        1                  as source_priority,
        coalesce(try_cast(substr(ancestor_qid, 2) as bigint), 0) as qid_number
    from {{ ref('stg_wd_ancestors') }}
    where ancestor_rank_name in {{ comparison_ranks() }}
),

combined as (
    select * from self_as_ancestor
    union all
    select * from from_chain
),

ranked as (
    select
        *,
        row_number() over (
            partition by wikidata_qid, rank_name
            order by source_priority, qid_number, ancestor_name
        ) as rn
    from combined
)

select
    wikidata_qid,
    rank_name,
    ancestor_name
from ranked
where rn = 1
