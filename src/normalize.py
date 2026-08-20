"""Scientific-name normalisation, applied identically to both the iNat and Wikidata sides. See docs/inat-wikidata-match-spec.md §1."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_HYBRID_MARKERS = {"x", "X", "×"}

_INFRA_CONNECTORS = {
    "subsp": "subsp",
    "ssp": "subsp",
    "var": "var",
    "f": "f",
    "forma": "f",
    "cv": "cv",
    "cultivar": "cv",
}

_GENUS_RE = re.compile(r"^[A-Za-z][A-Za-z-]*$")
_LOWER_WORD_RE = re.compile(r"^[a-z-]+$")


@dataclass(frozen=True)
class NormalizedName:
    raw: str
    normalized: str
    genus: str | None
    specific_epithet: str | None
    infraspecific_rank: str | None
    infraspecific_epithet: str | None
    hybrid: bool
    epithet_stem: str | None


def strip_diacritics(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def epithet_stem(word: str) -> str:
    """Drop the trailing gender marker so e.g. 'ruber'/'rubra'/'rubrum' collapse to one stem."""
    if word.endswith("er") and len(word) > 3:
        return word[:-2] + word[-1]
    if word.endswith(("um", "us", "is")):
        return word[:-2]
    if word.endswith(("a", "e")):
        return word[:-1]
    return word


def _split_hybrid_marker(tokens: list[str]) -> tuple[list[str], bool]:
    hybrid = False
    kept: list[str] = []
    for tok in tokens:
        if tok in _HYBRID_MARKERS:
            hybrid = True
            continue
        if tok.startswith("×"):
            hybrid = True
            tok = tok[1:]
        kept.append(tok)
    return kept, hybrid


def normalize_name(raw: str) -> NormalizedName:
    if not raw or not raw.strip():
        return NormalizedName(raw or "", "", None, None, None, None, False, None)

    cleaned = strip_diacritics(raw)
    tokens, hybrid = _split_hybrid_marker(cleaned.split())

    parts: list[str] = []
    genus: str | None = None
    specific_epithet: str | None = None
    infraspecific_rank: str | None = None
    infraspecific_epithet: str | None = None

    for i, tok in enumerate(tokens):
        connector_key = tok.rstrip(".").lower()
        if i > 0 and connector_key in _INFRA_CONNECTORS:
            infraspecific_rank = _INFRA_CONNECTORS[connector_key]
            parts.append(infraspecific_rank)
            continue

        if i == 0:
            if not _GENUS_RE.fullmatch(tok):
                break
            genus = tok.lower()
            parts.append(genus)
            continue

        if _LOWER_WORD_RE.fullmatch(tok):
            if infraspecific_rank is not None and infraspecific_epithet is None:
                infraspecific_epithet = tok
                parts.append(tok)
                continue
            if specific_epithet is None:
                specific_epithet = tok
                parts.append(tok)
                continue
            # Extra lowercase token beyond the expected structure: stop, treat as authorship.
            break

        # Anything else (capitalised surname, "L.", a year, "&", ...) starts the authorship.
        break

    normalized = " ".join(parts)
    stem_source = specific_epithet or infraspecific_epithet
    stem = epithet_stem(stem_source) if stem_source else None

    return NormalizedName(
        raw=raw,
        normalized=normalized,
        genus=genus,
        specific_epithet=specific_epithet,
        infraspecific_rank=infraspecific_rank,
        infraspecific_epithet=infraspecific_epithet,
        hybrid=hybrid,
        epithet_stem=stem,
    )
