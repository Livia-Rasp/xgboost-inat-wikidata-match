{#
    The three ranks kingdom_match / family_match / order_match compare on, mirroring
    features.COMPARISON_RANKS. One definition, used by both ancestor models, so the two sides of
    the comparison cannot drift apart.
#}
{% macro comparison_ranks() %}('kingdom', 'family', 'order'){% endmacro %}
