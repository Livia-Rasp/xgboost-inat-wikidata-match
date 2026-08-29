-- One row per *distinct* name string across both sides, parsed by the normalize_name() UDF
-- (src/dbt_udf.py). Distinct on purpose: the same ~650k strings would otherwise be parsed 1.2M
-- times, once per candidate row per side.
--
-- token_count and normalized_length are materialised here rather than in fct_features because
-- both are properties of the parse, and token_count needs a guard SQL does not give for free:
-- Python's "".split() is [] (0 tokens) while string_split('', ' ') is [''] (1).
with all_names as (
    select wikidata_name as raw_name from {{ ref('stg_wd_taxa') }}
    union
    select inat_name as raw_name from {{ ref('stg_candidates') }}
),

parsed as (
    select
        raw_name,
        normalize_name(raw_name) as parts
    from all_names
)

select
    raw_name,
    parts.normalized                                            as normalized,
    parts.genus                                                 as genus,
    parts.specific_epithet                                      as specific_epithet,
    parts.infraspecific_rank                                    as infraspecific_rank,
    parts.infraspecific_epithet                                 as infraspecific_epithet,
    parts.hybrid                                                as hybrid,
    parts.epithet_stem                                          as epithet_stem,
    length(parts.normalized)                                    as normalized_length,
    case
        when parts.normalized = '' then 0
        else length(string_split(parts.normalized, ' '))
    end                                                         as token_count
from parsed
