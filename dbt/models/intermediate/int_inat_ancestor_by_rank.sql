-- The iNat side of the same comparison, walking each candidate's slash-joined `ancestry` string.
--
-- Unlike the Wikidata side this one is already deterministic in pandas: it walks the string
-- left to right and keeps the first hit at each rank (src/features.py:98-111), so `position`
-- reproduces that exactly rather than substituting a new rule. The row's own rank still wins
-- over anything in its ancestry, same self-as-ancestor convention.
--
-- Scoped to taxa that actually appear as candidates — a few tens of thousands of the 1.4M —
-- while the id → (name, rank) lookup stays over the whole index, since a walk can reach any node.
with candidate_taxa as (
    select distinct inat_taxon_id
    from {{ ref('stg_candidates') }}
),

rows_to_walk as (
    select t.*
    from {{ ref('stg_inat_taxa') }} as t
    join candidate_taxa using (inat_taxon_id)
),

self_as_ancestor as (
    select
        inat_taxon_id,
        inat_rank as rank_name,
        inat_name as ancestor_name,
        0         as source_priority,
        0         as position
    from rows_to_walk
    where inat_rank in {{ comparison_ranks() }}
),

exploded as (
    select
        inat_taxon_id,
        unnest(string_split(ancestry, '/'))                    as ancestor_taxon_id,
        generate_subscripts(string_split(ancestry, '/'), 1)    as position
    from rows_to_walk
    where ancestry <> ''
),

from_chain as (
    select
        e.inat_taxon_id,
        a.inat_rank as rank_name,
        a.inat_name as ancestor_name,
        1           as source_priority,
        e.position
    from exploded as e
    join {{ ref('stg_inat_taxa') }} as a
        on a.inat_taxon_id = e.ancestor_taxon_id
    where a.inat_rank in {{ comparison_ranks() }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by inat_taxon_id, rank_name
            order by source_priority, position
        ) as rn
    from (
        select * from self_as_ancestor
        union all
        select * from from_chain
    )
)

select
    inat_taxon_id,
    rank_name,
    ancestor_name
from ranked
where rn = 1
