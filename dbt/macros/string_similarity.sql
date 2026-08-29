{#
    DuckDB's jaro_winkler_similarity and levenshtein stand in for rapidfuzz here — the deliberate
    substitution platform-design §2.2 accepts. On ASCII input they agree: measured on 30,000 real
    pairs, levenshtein exactly and jaro_winkler to within 5.6e-17 (one ULP of a 64-bit float).

    **Both DuckDB functions count bytes; rapidfuzz counts code points.** On non-ASCII input they
    therefore disagree outright — jaro_winkler_similarity('abc', 'ab×c') is 0.689 in DuckDB and
    0.933 in rapidfuzz. Every normalised name is ASCII by construction (normalize.py's genus and
    epithet patterns are `[A-Za-z-]`), so this only reaches parent_name_jw, the one feature
    computed on raw strings. Measured and explained in docs/findings.md §9, not worked around.

    strlen() rather than length() in the denominator for exactly that reason: strlen is bytes,
    which is the unit levenshtein() returns. Mixing the two would be a latent bug the moment a
    non-ASCII string reached this macro.

    The guards are not decoration. `_jw()` (src/features.py:115-118) returns 0.0 when either side
    is falsy, and levenshtein_ratio_full does the same, so an unparseable name scores 0 rather
    than propagating a null into a feature column XGBoost would then read as missing.
#}

{% macro jaro_winkler(left, right) -%}
case
    when {{ left }} is null or {{ left }} = '' or {{ right }} is null or {{ right }} = '' then 0.0
    else jaro_winkler_similarity({{ left }}, {{ right }})
end
{%- endmacro %}

{% macro levenshtein_ratio(left, right) -%}
case
    when {{ left }} is null or {{ left }} = '' or {{ right }} is null or {{ right }} = '' then 0.0
    else 1.0 - levenshtein({{ left }}, {{ right }})::double
              / greatest(strlen({{ left }}), strlen({{ right }}))
end
{%- endmacro %}
