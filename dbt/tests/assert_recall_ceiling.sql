-- Spec §7 milestone 3: the true match must be in the candidate set for ≥97% of positive groups.
-- This caps everything downstream, so it is worth re-measuring on every build rather than trusting
-- the number recorded once in the README.
--
-- Two deliberate choices:
--
-- * `inat_taxon_id = true_inat_id` rather than `label = 1`. The synthetic dropout zeroes the label
--   on 15% of resolvable groups (spec §3), and those are not candidate-generation misses. The
--   question here is whether generation *found* the row, which the label no longer answers.
-- * Scoped to items whose P3151 target still exists as an active iNat taxon. 12.85% of links point
--   at taxon ids that do not, and no strategy can reach a row that is not in the index — the
--   README reports both the raw and the resolvable-only number for the same reason.
--
-- Warn rather than error: a drop here is a signal to investigate candidate generation, which is
-- upstream in Python and untouched by this project — not a reason to fail a build of the feature
-- table, which is downstream of it and blameless.
{{ config(severity = 'warn') }}

with resolvable as (
    select distinct f.wikidata_qid
    from {{ ref('fct_features') }} as f
    join {{ ref('stg_inat_taxa') }} as t
        on t.inat_taxon_id = f.true_inat_id
),

found as (
    select distinct wikidata_qid
    from {{ ref('fct_features') }}
    where inat_taxon_id = true_inat_id
)

select
    count(*)                                              as n_resolvable,
    count(f.wikidata_qid)                                 as n_found,
    count(f.wikidata_qid)::double / nullif(count(*), 0)   as recall
from resolvable as r
left join found as f using (wikidata_qid)
having count(f.wikidata_qid)::double / nullif(count(*), 0) < 0.97
