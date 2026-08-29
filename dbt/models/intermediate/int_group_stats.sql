-- Group context: how a candidate compares to the other candidates for the same Wikidata item.
--
-- sim_rank_in_group is where the second row-order dependency lives. Pandas ranks with
-- method="first" (src/features.py:208), so ties break on candidates.parquet's row order — which
-- comes out of an imap_unordered pool and is therefore not stable across regenerations even on
-- the pandas side. Ties are common: every exact-match candidate scores similarity = 1.0. The
-- rule here is explicit and reproducible — highest similarity first, then lowest inat_taxon_id.
--
-- sim_margin_to_runner_up is the group's *second* order statistic subtracted from every row,
-- so it is negative below second place, and 0 for a single-candidate group (whose runner-up is
-- itself). That mirrors pandas' `s.nlargest(2).min() if len(s) > 1 else s.iloc[0]`.
with ranked as (
    select
        wikidata_qid,
        inat_taxon_id,
        similarity,
        row_number() over (
            partition by wikidata_qid
            order by similarity desc, inat_taxon_id
        ) as sim_rank_in_group,
        count(*) over (partition by wikidata_qid) as n_candidates
    from {{ ref('stg_candidates') }}
),

runner_up as (
    select
        wikidata_qid,
        similarity as runner_up_similarity
    from ranked
    where sim_rank_in_group = 2
)

select
    r.wikidata_qid,
    r.inat_taxon_id,
    r.n_candidates,
    r.sim_rank_in_group,
    r.similarity - coalesce(u.runner_up_similarity, r.similarity) as sim_margin_to_runner_up
from ranked as r
left join runner_up as u
    on r.wikidata_qid = u.wikidata_qid
