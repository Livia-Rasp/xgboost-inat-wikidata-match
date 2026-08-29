-- Candidate generation stays in Python (platform-design §2.3): candidates.py's chunked-trigram
-- FTS5 search has no DuckDB equivalent, and reimplementing it would put milestone 3's recall
-- ceiling at risk to widen a lineage graph. This is the row identity for everything downstream.
select
    wikidata_qid,
    inat_taxon_id,
    inat_name,
    inat_rank,
    strategies,
    similarity
from {{ source('caches', 'candidates') }}
