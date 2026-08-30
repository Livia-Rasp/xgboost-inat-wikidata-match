-- One row per candidate pair, with every column src/features.py's build_features_and_splits()
-- produces, in the order that function produces them.
--
-- This is the **canonical** feature table — what train.py, evaluate.py and build_figures.py read
-- (spec §7 milestone 15, platform-design §2.2).
--
-- Milestone 14 wrote it beside data/features.parquet rather than over it, because that milestone
-- only *measured* the drift and overwriting the comparand would have destroyed what the parity
-- report compares to. Milestone 15 released the freeze and aligned the two paths — all 52 columns
-- and the row order now agree — so the promotion changes no value, and
-- `python -m src.features` writes data/features_pandas.parquet as the comparand instead.
{{ config(
    materialized = 'external',
    location = env_var('MATCHER_DATA_DIR', 'data') ~ '/features.parquet'
) }}

with pairs as (
    select
        c.wikidata_qid,
        c.inat_taxon_id,
        c.inat_name,
        c.inat_rank,
        c.strategies,
        c.similarity,
        w.wikidata_name,
        w.rank_qid,
        w.parent_name,
        w.sitelinks,
        w.statements,
        w.iucn_qid,
        w.has_commons_cat
    from {{ ref('stg_candidates') }} as c
    join {{ ref('stg_wd_taxa') }} as w
        on c.wikidata_qid = w.wikidata_qid
),

-- The three rank-name columns each side contributes, pivoted from the long ancestor models.
wd_ancestors as (
    select
        wikidata_qid,
        max(ancestor_name) filter (where rank_name = 'kingdom') as wd_kingdom,
        max(ancestor_name) filter (where rank_name = 'family')  as wd_family,
        max(ancestor_name) filter (where rank_name = 'order')   as wd_order
    from {{ ref('int_ancestor_by_rank') }}
    group by 1
),

inat_ancestors as (
    select
        inat_taxon_id,
        max(ancestor_name) filter (where rank_name = 'kingdom') as inat_kingdom,
        max(ancestor_name) filter (where rank_name = 'family')  as inat_family,
        max(ancestor_name) filter (where rank_name = 'order')   as inat_order
    from {{ ref('int_inat_ancestor_by_rank') }}
    group by 1
),

inat_collisions as (
    select name, n_taxa_same_name from {{ ref('int_name_collisions') }} where side = 'inat'
),

wd_collisions as (
    select name, n_taxa_same_name from {{ ref('int_name_collisions') }} where side = 'wikidata'
),

joined as (
    select
        p.*,
        wn.normalized             as wd_normalized,
        wn.genus                  as wd_genus,
        wn.specific_epithet       as wd_epithet,
        wn.infraspecific_rank     as wd_infra_rank,
        wn.infraspecific_epithet  as wd_infra_epithet,
        wn.hybrid                 as wd_hybrid,
        wn.epithet_stem           as wd_epithet_stem,
        wn.normalized_length      as wd_normalized_length,
        wn.token_count            as wd_token_count,
        inn.normalized            as inat_normalized,
        inn.genus                 as inat_genus,
        inn.specific_epithet      as inat_epithet,
        inn.infraspecific_rank    as inat_infra_rank,
        inn.infraspecific_epithet as inat_infra_epithet,
        inn.hybrid                as inat_hybrid,
        inn.epithet_stem          as inat_epithet_stem,
        inn.normalized_length     as inat_normalized_length,
        inn.token_count           as inat_token_count,
        wr.rank_name              as wd_rank_name,
        wr.rank_level             as wd_rank_level,
        ir.rank_level             as inat_rank_level,
        wa.wd_kingdom,
        wa.wd_family,
        wa.wd_order,
        ia.inat_kingdom,
        ia.inat_family,
        ia.inat_order,
        coalesce(ic.n_taxa_same_name, 0) as n_inat_taxa_same_name,
        coalesce(wc.n_taxa_same_name, 0) as n_wikidata_items_same_name,
        g.n_candidates,
        g.sim_rank_in_group,
        g.sim_margin_to_runner_up,
        l.true_inat_id,
        l.label,
        l.no_answer_reason,
        f.family_key,
        f.fold
    from pairs as p
    -- `is not distinct from`, not `=`: one Wikidata item really has no label (Q2125371), and
    -- normalize_name() gives a null name the same degenerate parse it gives a blank one. An
    -- equality join would drop that row's parse instead of matching it.
    left join {{ ref('int_name_parts') }} as wn  on p.wikidata_name is not distinct from wn.raw_name
    left join {{ ref('int_name_parts') }} as inn on p.inat_name is not distinct from inn.raw_name
    left join {{ ref('stg_rank_names') }} as wr  on p.rank_qid = wr.rank_qid
    left join {{ ref('stg_rank_levels') }} as ir on p.inat_rank = ir.rank_name
    left join wd_ancestors as wa                 on p.wikidata_qid = wa.wikidata_qid
    left join inat_ancestors as ia               on p.inat_taxon_id = ia.inat_taxon_id
    left join inat_collisions as ic              on p.inat_name = ic.name
    left join wd_collisions as wc                on p.wikidata_name = wc.name
    left join {{ ref('int_group_stats') }} as g
        on p.wikidata_qid = g.wikidata_qid and p.inat_taxon_id = g.inat_taxon_id
    left join {{ ref('int_labels') }} as l
        on p.wikidata_qid = l.wikidata_qid and p.inat_taxon_id = l.inat_taxon_id
    left join {{ ref('dim_folds') }} as f        on p.wikidata_qid = f.wikidata_qid
),

matches as (
    select
        *,
        -- `w is not None and w == i`: a null on either side is not a match, and two nulls are
        -- certainly not one. coalesce(..., false) is what gives that, since `null = null` is null.
        coalesce(wd_kingdom = inat_kingdom, false) as kingdom_match,
        coalesce(wd_family = inat_family, false)   as family_match,
        coalesce(wd_order = inat_order, false)     as order_match
    from joined
)

select
    wikidata_qid,
    inat_taxon_id,
    inat_name,
    inat_rank,
    strategies,
    similarity,
    true_inat_id,
    label,

    -- ---- String similarity (spec §4) ----
    wikidata_name = inat_name                                       as name_exact_raw,
    wd_normalized = inat_normalized                                 as name_exact_norm,
    {{ jaro_winkler('wd_normalized', 'inat_normalized') }}          as jaro_winkler_full,
    {{ levenshtein_ratio('wd_normalized', 'inat_normalized') }}     as levenshtein_ratio_full,
    coalesce(wd_genus = inat_genus, false)                          as genus_exact,
    {{ jaro_winkler('wd_genus', 'inat_genus') }}                    as genus_jw,
    coalesce(wd_epithet = inat_epithet, false)                      as epithet_exact,
    {{ jaro_winkler('wd_epithet', 'inat_epithet') }}                as epithet_jw,
    coalesce(wd_epithet_stem = inat_epithet_stem, false)            as epithet_stem_match,
    abs(wd_token_count - inat_token_count)                          as token_count_diff,
    abs(wd_normalized_length - inat_normalized_length)              as length_diff,
    -- Two nulls *do* match here: `a.infraspecific_rank == b.infraspecific_rank` with no not-null
    -- guard (features.py:171), so two names with no infraspecific part agree. Same for hybrid.
    wd_infra_rank is not distinct from inat_infra_rank              as infra_rank_match,
    coalesce(wd_infra_epithet = inat_infra_epithet, false)          as infra_epithet_match,
    wd_hybrid = inat_hybrid                                         as hybrid_flag_match,

    -- ---- Taxonomic agreement ----
    coalesce(wd_rank_name = inat_rank, false)                       as rank_equal,
    abs(wd_rank_level - inat_rank_level)::double                    as rank_level_diff,
    kingdom_match,
    family_match,
    order_match,
    (kingdom_match::int + family_match::int + order_match::int)::bigint
                                                                    as shared_ancestor_depth,
    -- Wikidata's *parent* label against the iNat candidate's *own* name, on the raw strings.
    -- rapidfuzz via a UDF, not DuckDB's jaro_winkler_similarity: this is the one feature
    -- computed on *raw* names, and DuckDB counts UTF-8 bytes where rapidfuzz counts code
    -- points. See src/dbt_udf.jaro_winkler_codepoints.
    jaro_winkler_codepoints(parent_name, inat_name)                 as parent_name_jw,
    true                                                            as inat_active,

    -- ---- Group context ----
    n_candidates,
    n_inat_taxa_same_name,
    n_wikidata_items_same_name,
    sim_rank_in_group::double                                       as sim_rank_in_group,
    sim_margin_to_runner_up,
    {% for tag in var('strategy_tags') -%}
    contains(strategies, '{{ tag }}')                               as strategy_{{ tag }},
    {% endfor %}

    -- ---- Popularity / quality ----
    sitelinks                                                       as wikidata_sitelink_count,
    statements                                                      as wikidata_statement_count,
    iucn_qid is not null                                            as wikidata_has_iucn,
    has_commons_cat                                                 as wikidata_has_commons_cat,

    no_answer_reason,
    family_key,
    fold
from matches
-- Deterministic row order, matching build_features_and_splits'.
--
-- Not cosmetic: TREE_PARAMS's subsample=0.8 selects rows by *position*, so the order this table
-- is written in is part of the trained model. SQL guarantees no order without an order by, and
-- this table is about to become the canonical one train.py reads — without this line, making it
-- canonical would reintroduce exactly the irreproducibility the pandas path just had fixed.
order by wikidata_qid, inat_taxon_id
