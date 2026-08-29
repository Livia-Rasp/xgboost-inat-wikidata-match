-- The Wikidata side of a pair. Only the columns features.build_features() actually joins in
-- (src/features.py:136-144), plus inat_id, which is the P3151 label build_labels() reads.
select
    qid                as wikidata_qid,
    name               as wikidata_name,
    inat_id            as true_inat_id,
    rank_qid,
    parent_name,
    sitelinks,
    statements,
    iucn_qid,
    has_commons_cat
from {{ source('caches', 'wikidata_taxa') }}
