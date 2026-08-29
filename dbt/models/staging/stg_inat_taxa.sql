-- The attached SQLite index, read in place. taxon_id stays VARCHAR: it is a TEXT column upstream
-- and an integer cast would break every join downstream (src/features.py:42-44 records the same
-- hazard on the pandas side).
select
    taxon_id             as inat_taxon_id,
    name                 as inat_name,
    rank                 as inat_rank,
    coalesce(ancestry, '') as ancestry
from {{ source('lookup', 'taxa_normalized') }}
