{#
    A range test. dbt_utils has one, but pulling in a package would mean `dbt deps` (and a network
    fetch) before `dbt build` could run at all — this project's whole five-minute path is built on
    not needing that. Twelve lines is cheaper than the dependency.

    Nulls pass: `not_null` is a separate test, and a column that is legitimately nullable
    (rank_level_diff) should not fail a range check for being null.
#}
{% test between(model, column_name, min_value, max_value) %}

select {{ column_name }}
from {{ model }}
where {{ column_name }} is not null
  and ({{ column_name }} < {{ min_value }} or {{ column_name }} > {{ max_value }})

{% endtest %}
