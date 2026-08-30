"""dbt-duckdb plugin registering this project's name normalisation as a DuckDB UDF.

Spec §7 milestone 14 moves feature construction into SQL. Ten of the 41 features derive from
`normalize.py`'s parse of a scientific name (genus, epithet, infraspecific rank/epithet, hybrid
flag, gender stem), and that parse is a token-by-token state machine with `break` semantics, not
a regex — `docs/platform-design.md` §5.2 originally proposed transcribing it into
`regexp_extract`. Registering the real function instead keeps `normalize.py` the single source of
truth, so the parity report measures the drift that is *interesting* (the deliberate ones in
§2.2) rather than a tail of transcription bugs in a parser nobody wanted to change.

The UDF is applied to distinct name strings only (see models/intermediate/int_name_parts.sql),
not once per candidate row, so the Python call overhead is paid ~650k times rather than 1.2M.
"""

from __future__ import annotations

from typing import Any

from dbt.adapters.duckdb.plugins import BasePlugin
from duckdb import DuckDBPyConnection
from rapidfuzz.distance import JaroWinkler

from .normalize import normalize_name

# Field order matches NormalizedName minus `raw`, which SQL already has in hand.
# Nullability is deliberately not declared: DuckDB creates a UDF with a `... VARCHAR NULL` return
# struct and then fails at call time (duckdb#18600). Every field here is nullable in practice.
NAME_PARTS_TYPE = (
    "STRUCT("
    "normalized VARCHAR, "
    "genus VARCHAR, "
    "specific_epithet VARCHAR, "
    "infraspecific_rank VARCHAR, "
    "infraspecific_epithet VARCHAR, "
    "hybrid BOOLEAN, "
    "epithet_stem VARCHAR"
    ")"
)


def normalize_name_struct(raw: str | None) -> dict[str, Any]:
    """`normalize_name()` as a flat struct. Registered with null_handling='special' so a NULL
    name reaches Python and takes the same degenerate-input branch a blank string does, rather
    than DuckDB short-circuiting to NULL — that branch returns normalized='' and NULL parts,
    which is what the pandas path produces and what the feature guards are written against."""
    parsed = normalize_name(raw)
    return {
        "normalized": parsed.normalized,
        "genus": parsed.genus,
        "specific_epithet": parsed.specific_epithet,
        "infraspecific_rank": parsed.infraspecific_rank,
        "infraspecific_epithet": parsed.infraspecific_epithet,
        "hybrid": parsed.hybrid,
        "epithet_stem": parsed.epithet_stem,
    }


def jaro_winkler_codepoints(left: str | None, right: str | None) -> float:
    """rapidfuzz's Jaro-Winkler, for the one feature computed on raw rather than normalised names.

    DuckDB's `jaro_winkler_similarity` agrees with rapidfuzz to one ULP (5.55e-17 over all 590,671
    rows) — but it counts UTF-8 **bytes** where rapidfuzz counts **code points**, so the two
    diverge on any non-ASCII input: `jaro_winkler_similarity('abc','ab×c')` is 0.689 against
    rapidfuzz's 0.933.

    Every normalised name is ASCII, because normalize.py's genus/epithet patterns are [A-Za-z-],
    so only `parent_name_jw` is exposed — it compares Wikidata's raw parent label against the
    iNat candidate's raw name. That was 3,976 rows, 0.67%, every one of them involving a
    non-ASCII name. Calling the same library on both sides removes the last feature-level
    disagreement between the two paths, which is what lets the SQL table become canonical without
    skewing gold scoring (which is always built by the pandas path).
    """
    if not left or not right:
        return 0.0
    # normalized_similarity, matching features._jw exactly — not .similarity.
    return JaroWinkler.normalized_similarity(left, right)


class Plugin(BasePlugin):
    def configure_connection(self, conn: DuckDBPyConnection) -> None:
        conn.create_function(
            "normalize_name",
            normalize_name_struct,
            ["VARCHAR"],
            NAME_PARTS_TYPE,
            null_handling="special",
        )
        conn.create_function(
            "jaro_winkler_codepoints",
            jaro_winkler_codepoints,
            ["VARCHAR", "VARCHAR"],
            "DOUBLE",
            null_handling="special",
        )
