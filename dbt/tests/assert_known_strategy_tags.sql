-- Every tag in a `strategies` string must be one of candidates.STRATEGY_TAGS.
--
-- accepted_values cannot express this: `strategies` is a pipe-joined subset, not a single value.
-- It matters because the ten strategy_* feature columns are generated from that fixed list, so a
-- tag candidates.py started emitting but nobody added to the list would vanish from the feature
-- set silently rather than raising anything.
select distinct tag
from (
    select unnest(string_split(strategies, '|')) as tag
    from {{ ref('stg_candidates') }}
)
where tag not in (
    {%- for tag in var('strategy_tags') %}
    '{{ tag }}'{{ "," if not loop.last }}
    {%- endfor %}
)
