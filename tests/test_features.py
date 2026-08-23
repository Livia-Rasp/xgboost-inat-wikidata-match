"""Pairwise features (spec §4), with most of the weight on the taxonomic-agreement group.

Those four — kingdom_match, family_match, order_match, shared_ancestor_depth — are the features
that separate *Prunella* the bird from *Prunella* the mint, and they are computed by walking two
different ancestry representations (Wikidata's transitive P171 rows, iNaturalist's slash-joined
id string). A bug in either walk produces plausible-looking zeros rather than an error, so
nothing else in the pipeline would notice.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.features import (
    _inat_ancestor_names_by_rank,
    _wd_ancestor_names_by_rank,
    build_features,
)

# Two Wikidata items with the same name string and different kingdoms: exactly the hemihomonym
# the taxonomic features exist to resolve.
WIKIDATA_TAXA = pd.DataFrame([
    {"qid": "Q_BIRD", "name": "Prunella", "rank_qid": "Q34740", "parent_qid": "Q_BIRDFAM",
     "parent_name": "Prunellidae", "iucn_qid": None, "sitelinks": 30, "statements": 40,
     "has_commons_cat": True},
    {"qid": "Q_MINT", "name": "Prunella", "rank_qid": "Q34740", "parent_qid": "Q_MINTFAM",
     "parent_name": "Lamiaceae", "iucn_qid": None, "sitelinks": 25, "statements": 35,
     "has_commons_cat": False},
])

ANCESTORS = pd.DataFrame([
    {"qid": "Q_BIRD", "ancestor_qid": "Q_BIRDFAM", "ancestor_name": "Prunellidae", "ancestor_rank_qid": "Q35409"},
    {"qid": "Q_BIRD", "ancestor_qid": "Q_BIRDORD", "ancestor_name": "Passeriformes", "ancestor_rank_qid": "Q36602"},
    {"qid": "Q_BIRD", "ancestor_qid": "Q_ANIMALIA", "ancestor_name": "Animalia", "ancestor_rank_qid": "Q36732"},
    {"qid": "Q_MINT", "ancestor_qid": "Q_MINTFAM", "ancestor_name": "Lamiaceae", "ancestor_rank_qid": "Q35409"},
    {"qid": "Q_MINT", "ancestor_qid": "Q_MINTORD", "ancestor_name": "Lamiales", "ancestor_rank_qid": "Q36602"},
    {"qid": "Q_MINT", "ancestor_qid": "Q_PLANTAE", "ancestor_name": "Plantae", "ancestor_rank_qid": "Q36732"},
])


def _candidates(qid: str) -> pd.DataFrame:
    """Both Prunella taxa as candidates for one Wikidata item — the real ambiguous group."""
    return pd.DataFrame([
        {"wikidata_qid": qid, "inat_taxon_id": "13982", "inat_name": "Prunella",
         "inat_rank": "genus", "strategies": "exact", "similarity": 1.0},
        {"wikidata_qid": qid, "inat_taxon_id": "52765", "inat_name": "Prunella",
         "inat_rank": "genus", "strategies": "exact", "similarity": 1.0},
    ])


@pytest.fixture
def bird_features(inat_index):
    return build_features(_candidates("Q_BIRD"), WIKIDATA_TAXA, ANCESTORS, inat_index).set_index("inat_taxon_id")


@pytest.fixture
def mint_features(inat_index):
    return build_features(_candidates("Q_MINT"), WIKIDATA_TAXA, ANCESTORS, inat_index).set_index("inat_taxon_id")


def test_kingdom_match_separates_the_hemihomonyms(bird_features, mint_features):
    assert bird_features.loc["13982", "kingdom_match"]      # bird item ↔ bird taxon
    assert not bird_features.loc["52765", "kingdom_match"]  # bird item ↔ mint taxon
    assert mint_features.loc["52765", "kingdom_match"]
    assert not mint_features.loc["13982", "kingdom_match"]


def test_family_and_order_match_agree_with_kingdom_match(bird_features):
    assert bird_features.loc["13982", "family_match"]
    assert bird_features.loc["13982", "order_match"]
    assert not bird_features.loc["52765", "family_match"]
    assert not bird_features.loc["52765", "order_match"]


def test_shared_ancestor_depth_counts_the_three_rank_matches(bird_features):
    assert bird_features.loc["13982", "shared_ancestor_depth"] == 3
    assert bird_features.loc["52765", "shared_ancestor_depth"] == 0


def test_string_similarity_alone_cannot_separate_them(bird_features):
    """The point of the taxonomic features: on name evidence the two candidates are identical."""
    for taxon_id in ("13982", "52765"):
        assert bird_features.loc[taxon_id, "name_exact_raw"]
        assert bird_features.loc[taxon_id, "jaro_winkler_full"] == 1.0


def test_an_item_at_a_target_rank_is_its_own_ancestor_there():
    """A family-rank item has no family *ancestor*, but its family is itself. Without this
    convention every family-rank item would score family_match=False against its own family."""
    taxa = pd.DataFrame([{
        "qid": "Q_FAM", "name": "Prunellidae", "rank_qid": "Q35409", "parent_qid": None,
        "parent_name": None, "iucn_qid": None, "sitelinks": 1, "statements": 1,
        "has_commons_cat": False,
    }])
    ranks = _wd_ancestor_names_by_rank(taxa, pd.DataFrame(
        columns=["qid", "ancestor_qid", "ancestor_name", "ancestor_rank_qid"]
    ))
    assert ranks["Q_FAM"]["family"] == "Prunellidae"


def test_the_inat_walk_resolves_ranks_through_the_ancestry_string(inat_index):
    ranks = _inat_ancestor_names_by_rank(inat_index, taxon_ids={"13982", "52765"})
    assert ranks["13982"] == {"kingdom": "Animalia", "order": "Passeriformes", "family": "Prunellidae"}
    assert ranks["52765"] == {"kingdom": "Plantae", "order": "Lamiales", "family": "Lamiaceae"}


def test_the_inat_walk_takes_the_nearest_ancestor_at_each_rank(inat_index):
    """`Acer rubrum var. tomentosum` sits below species, genus, family and order. The walk goes
    outward from the row itself, so it must return Sapindaceae rather than any coarser family."""
    ranks = _inat_ancestor_names_by_rank(inat_index, taxon_ids={"208"})
    assert ranks["208"]["family"] == "Sapindaceae"
    assert ranks["208"]["order"] == "Sapindales"
    assert ranks["208"]["kingdom"] == "Plantae"


def test_rank_equal_and_rank_level_diff(inat_index):
    """Q34740 is genus. The candidates are genus-rank, so ranks agree and the level gap is 0."""
    features = build_features(_candidates("Q_BIRD"), WIKIDATA_TAXA, ANCESTORS, inat_index)
    assert features["rank_equal"].all()
    assert (features["rank_level_diff"] == 0).all()


def test_rank_level_diff_grows_with_the_rank_gap(inat_index):
    candidates = pd.DataFrame([
        {"wikidata_qid": "Q_BIRD", "inat_taxon_id": "13983", "inat_name": "Prunella modularis",
         "inat_rank": "species", "strategies": "trigram", "similarity": 0.6},
    ])
    features = build_features(candidates, WIKIDATA_TAXA, ANCESTORS, inat_index)
    assert not features["rank_equal"].iloc[0]
    assert features["rank_level_diff"].iloc[0] == 10  # genus 60 → species 70


def test_group_context_features(inat_index):
    """n_candidates, the within-group similarity rank, and the margin to the runner-up — the
    third-most-important feature in the SHAP ordering, and entirely group-relative."""
    candidates = pd.DataFrame([
        {"wikidata_qid": "Q_BIRD", "inat_taxon_id": "13982", "inat_name": "Prunella",
         "inat_rank": "genus", "strategies": "exact", "similarity": 1.0},
        {"wikidata_qid": "Q_BIRD", "inat_taxon_id": "71358", "inat_name": "Prunellidae",
         "inat_rank": "family", "strategies": "trigram", "similarity": 0.7},
    ])
    features = build_features(candidates, WIKIDATA_TAXA, ANCESTORS, inat_index).set_index("inat_taxon_id")

    assert (features["n_candidates"] == 2).all()
    assert features.loc["13982", "sim_rank_in_group"] == 1
    assert features.loc["71358", "sim_rank_in_group"] == 2
    assert features.loc["13982", "sim_margin_to_runner_up"] == pytest.approx(0.3)
    assert features.loc["71358", "sim_margin_to_runner_up"] == pytest.approx(0.0)


def test_name_collision_counts_come_from_the_whole_index(inat_index):
    """n_inat_taxa_same_name is what tells the model 'this name is shared', so it counts across
    the entire index, not within the candidate group."""
    features = build_features(_candidates("Q_BIRD"), WIKIDATA_TAXA, ANCESTORS, inat_index)
    assert (features["n_inat_taxa_same_name"] == 2).all()   # two Prunella rows in the index
    assert (features["n_wikidata_items_same_name"] == 2).all()  # two Prunella items in WIKIDATA_TAXA


def test_strategy_flags_are_one_column_per_tag(inat_index):
    features = build_features(_candidates("Q_BIRD"), WIKIDATA_TAXA, ANCESTORS, inat_index)
    assert features["strategy_exact"].all()
    assert not features["strategy_trigram"].any()
