"""Spec §1's normalisation rules, one test per rule.

Everything downstream depends on this being applied identically to both sides, so a silent change
here moves every similarity feature at once without failing anything else.
"""

from __future__ import annotations

import pytest

from src.normalize import epithet_stem, normalize_name, strip_diacritics


def test_authorship_is_stripped():
    assert normalize_name("Rosa canina L.").normalized == "rosa canina"
    assert normalize_name("Acer rubrum Linnaeus, 1753").normalized == "acer rubrum"
    assert normalize_name("Prunella vulgaris L. & Sm.").normalized == "prunella vulgaris"


def test_genus_is_lowercased_and_parts_exposed():
    parsed = normalize_name("Acer rubrum")
    assert parsed.genus == "acer"
    assert parsed.specific_epithet == "rubrum"
    assert parsed.infraspecific_rank is None
    assert parsed.hybrid is False


def test_diacritics_are_stripped():
    assert strip_diacritics("Aethusa cynapioïdes") == "Aethusa cynapioides"
    assert normalize_name("Rubus küpferi").normalized == "rubus kupferi"
    assert normalize_name("Prunella vulgáris").normalized == "prunella vulgaris"


def test_ligatures_are_a_known_gap():
    """NFKD decomposes accents but not ligatures, so an æ/œ/ß fails the genus and lowercase-word
    character classes: in an epithet the epithet is dropped as if it were authorship, and in a
    genus the whole name parses empty.

    Pinned rather than fixed: there are zero such names in the 1.4M-row iNat index, and changing
    normalisation would invalidate every cached feature the frozen models were trained against.
    Recorded in docs/future-work.md instead of silently left as a surprise."""
    assert normalize_name("Sedum bæticum").specific_epithet is None
    assert normalize_name("Æthusa cynapioides").normalized == ""
    assert normalize_name("Sedum baeticum").specific_epithet == "baeticum"


@pytest.mark.parametrize(
    "raw, rank, epithet",
    [
        ("Acer rubrum subsp. tomentosum", "subsp", "tomentosum"),
        ("Acer rubrum ssp. tomentosum", "subsp", "tomentosum"),
        ("Acer rubrum var. tomentosum", "var", "tomentosum"),
        ("Acer rubrum f. tomentosum", "f", "tomentosum"),
        ("Acer rubrum forma tomentosum", "f", "tomentosum"),
        ("Acer rubrum cv. tomentosum", "cv", "tomentosum"),
    ],
)
def test_infraspecific_connectors_normalise(raw, rank, epithet):
    parsed = normalize_name(raw)
    assert parsed.infraspecific_rank == rank
    assert parsed.infraspecific_epithet == epithet
    assert parsed.normalized == f"acer rubrum {rank} {epithet}"


@pytest.mark.parametrize("raw", ["Acer x freemanii", "Acer × freemanii", "Acer ×freemanii"])
def test_hybrid_markers_set_the_flag_and_leave_the_name(raw):
    parsed = normalize_name(raw)
    assert parsed.hybrid is True
    assert parsed.normalized == "acer freemanii"


def test_non_hybrid_names_are_not_flagged():
    assert normalize_name("Acer rubrum").hybrid is False


@pytest.mark.parametrize(
    "a, b",
    [
        ("ruber", "rubra"),   # -er / -ra
        ("ruber", "rubrum"),  # -er / -um
        ("rubra", "rubrum"),  # -a / -um
        ("gracilis", "gracile"),  # -is / -e
    ],
)
def test_gender_variants_share_an_epithet_stem(a, b):
    """Latin gender agreement makes these the same epithet, and spec §1 requires them to
    collapse — Acer rubrum and Acer ruber are a real match, not a near-miss."""
    assert epithet_stem(a) == epithet_stem(b)


def test_unrelated_epithets_do_not_share_a_stem():
    assert epithet_stem("rubrum") != epithet_stem("saccharum")


def test_normalized_names_carry_the_stem():
    assert normalize_name("Acer rubrum").epithet_stem == normalize_name("Acer ruber").epithet_stem


def test_provisional_names_yield_no_epithet():
    """Candidate indexing drops species-rank rows with no parseable epithet (see
    candidates.build_lookup_cache) — this is the parse that decides that."""
    assert normalize_name("Acer sp. 'AZ19'").specific_epithet is None
    assert normalize_name("Acer sp.").specific_epithet is None


def test_degenerate_input_does_not_raise():
    for raw in ["", "   ", None, 42]:
        parsed = normalize_name(raw)
        assert parsed.normalized == ""
        assert parsed.genus is None


def test_a_name_that_does_not_start_with_a_genus_parses_empty():
    assert normalize_name("123 nonsense").normalized == ""
