-- Spec §7 milestone 4's literal acceptance check, as a test rather than a print statement.
--
-- The one property this migration is not allowed to move: every candidate row for a Wikidata item
-- must sit in the same fold, or the out-of-fold predictions leak. features.py has
-- verify_no_qid_split_across_folds() and tests/test_splits.py covers the pandas path; this covers
-- the SQL one, at real scale, on every build.
select
    wikidata_qid,
    count(distinct fold) as n_folds
from {{ ref('fct_features') }}
group by 1
having count(distinct fold) > 1
