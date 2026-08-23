"""Candidate generation (spec §2), against a real 26-row SQLite index built by conftest.

The fixture taxonomy is deliberately shaped around the cases the strategies exist for:

  * `Prunella` appears twice, as a bird genus (Prunellidae, Animalia) and a mint genus
    (Lamiaceae, Plantae) — the hemihomonym that is milestone 1's acceptance check;
  * `Acer rubrum` / `Acer ruber` / `Acer rubrium` cover gender agreement and orthographic
    variation within a genus;
  * `Acor rubrum` is a synthetic near-miss genus, for the epithet-fixed strategy;
  * `Acer sp. 'AZ19'` is a provisional name with no parseable epithet.
"""

from __future__ import annotations

from src.candidates import (
    K,
    _epithet_fuzzy_candidates,
    _exact_candidates,
    _genus_fuzzy_candidates,
    _trigram_candidates,
    generate_candidates,
    lookup_by_normalized_name,
)


def test_exact_lookup_returns_both_hemihomonyms(lookup):
    """Milestone 1's literal acceptance check: do not stop at the first hit."""
    rows = lookup_by_normalized_name(lookup, "prunella")
    assert {r["taxon_id"] for r in rows} == {"13982", "52765"}
    assert {r["ancestry"].split("/")[0] for r in rows} == {"1", "100"}


def test_exact_candidates_are_case_and_authorship_insensitive(lookup):
    assert {c["taxon_id"] for c in _exact_candidates(lookup, "acer rubrum")} == {"203"}
    # The caller normalises first, which is what makes "Acer rubrum L." reach the same row.
    from src.normalize import normalize_name

    assert normalize_name("Acer rubrum L.").normalized == "acer rubrum"


def test_genus_fixed_epithet_fuzzy_finds_orthographic_variants(lookup):
    """Strategy 2: same genus, epithet within Levenshtein distance 2."""
    found = {c["taxon_id"] for c in _genus_fuzzy_candidates(lookup, "acer", "rubrum")}
    assert "203" in found  # Acer rubrum, distance 0
    assert "205" in found  # Acer rubrium, distance 1
    assert "206" not in found  # Acer saccharum, distance 6


def test_gender_variants_are_out_of_reach_of_the_edit_distance(lookup):
    """`rubrum` → `ruber` is Levenshtein distance 3, past strategy 2's threshold of 2. Gender
    agreement is handled downstream by the `epithet_stem_match` feature, not by this strategy —
    worth pinning, because it is easy to assume the fuzzy strategy covers it and it does not."""
    from src.normalize import epithet_stem

    assert "204" not in {c["taxon_id"] for c in _genus_fuzzy_candidates(lookup, "acer", "rubrum")}
    assert epithet_stem("rubrum") == epithet_stem("ruber")
    # It still reaches the candidate set, via the trigram strategy.
    assert "204" in {c["taxon_id"] for c in generate_candidates(lookup, "Acer rubrum")}


def test_epithet_fixed_genus_fuzzy_finds_a_near_miss_genus(lookup):
    """Strategy 3: same epithet, genus within Levenshtein distance 2."""
    found = {c["taxon_id"] for c in _epithet_fuzzy_candidates(lookup, "rubrum", "acer")}
    assert "207" in found  # Acor rubrum, genus distance 1
    assert "211" not in found  # Rosa canina shares neither


def test_trigram_search_ranks_the_exact_name_first(lookup):
    hits = _trigram_candidates(lookup, "prunella")
    assert hits, "trigram search returned nothing"
    assert {h["taxon_id"] for h in hits} >= {"13982", "52765"}
    assert "71358" in {h["taxon_id"] for h in hits}  # Prunellidae shares 6-char chunks


def test_generate_candidates_tags_every_strategy_that_found_a_row(lookup):
    candidates = generate_candidates(lookup, "Acer rubrum")
    by_id = {c["taxon_id"]: c for c in candidates}

    assert "exact" in by_id["203"]["strategies"]
    assert "genus_epithet_fuzzy" in by_id["205"]["strategies"]
    assert "epithet_genus_fuzzy" in by_id["207"]["strategies"]
    assert "trigram" in by_id["204"]["strategies"]
    # A row reachable by more than one strategy is merged, not duplicated.
    assert len(candidates) == len(by_id)
    assert "genus_epithet_fuzzy" in by_id["203"]["strategies"]


def test_generate_candidates_scores_the_exact_match_highest(lookup):
    candidates = generate_candidates(lookup, "Acer rubrum")
    best = max(candidates, key=lambda c: c["similarity"])
    assert best["taxon_id"] == "203"


def test_generate_candidates_respects_k(lookup):
    assert len(generate_candidates(lookup, "Acer rubrum", k=2)) == 2
    assert len(generate_candidates(lookup, "Acer rubrum")) <= K


def test_synonyms_reach_rows_the_primary_name_cannot(lookup):
    """Strategy 5. The Wikidata item is named Prunella, so nothing about `Acer rubrum` is
    reachable from the primary name — only from the synonym."""
    plain = {c["taxon_id"] for c in generate_candidates(lookup, "Prunella")}
    with_synonym = {
        c["taxon_id"] for c in generate_candidates(lookup, "Prunella", synonym_names=["Acer rubrum"])
    }
    assert "203" not in plain
    assert "203" in with_synonym

    tagged = [c for c in generate_candidates(lookup, "Prunella", synonym_names=["Acer rubrum"])
              if c["taxon_id"] == "203"]
    assert "synonym_exact" in tagged[0]["strategies"]


def test_provisional_names_are_excluded_from_the_index(lookup):
    """A species-rank row whose name has no parseable epithet has no stable identity to match
    against, so build_lookup_cache drops it (~4.5k rows at full scale)."""
    present = {r[0] for r in lookup.execute("SELECT taxon_id FROM taxa_normalized")}
    assert "210" not in present  # Acer sp. 'AZ19'
    assert "203" in present
    # Non-species ranks are kept even without an epithet — that is what genus rows are.
    assert "202" in present


def test_a_name_with_no_match_returns_no_candidates(lookup):
    assert generate_candidates(lookup, "Zzyzxia nonexistentia") == []
