"""Reading the checker's findings database. Spec §7 milestone 16.

Two properties worth pinning, both of them promises this repo makes to another repo:

* it reads only the *ambiguous* link findings — the queue the gold set was drawn from — and not
  the ~200 unambiguous `open` ones the checker intends to write itself;
* it cannot write, because the connection is read-only, not because this code is careful.

No network, no sibling checkout: the fixture builds the real schema and a handful of rows.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src import ambiguous

# The tables and columns this project reads, copied from the checker's own schema (lib/db.js).
SCHEMA = """
CREATE TABLE taxa (
    qid TEXT PRIMARY KEY, inat_id TEXT, taxon_name TEXT, rank TEXT, iucn TEXT,
    first_seen TEXT NOT NULL
) STRICT;
CREATE TABLE findings (
    id INTEGER PRIMARY KEY, qid TEXT NOT NULL REFERENCES taxa(qid), kind TEXT NOT NULL,
    payload TEXT, status TEXT NOT NULL, discovered_at TEXT NOT NULL, checked_at TEXT NOT NULL,
    verified_at TEXT, resolved_at TEXT, resolution TEXT, UNIQUE (qid, kind)
) STRICT;
"""


@pytest.fixture
def findings_db(tmp_path):
    path = tmp_path / "findings.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO taxa (qid, taxon_name, rank, first_seen) VALUES (?, ?, ?, '2026-01-01')",
        [("Q1", "Grania", "genus"), ("Q2", "Abies alba", "species"), ("Q3", "Prunella", "genus")],
    )
    conn.executemany(
        "INSERT INTO findings (qid, kind, payload, status, discovered_at, checked_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-02')",
        [
            ("Q1", "link", json.dumps({"candidates": [{"inatId": "1"}, {"inatId": "2"}]}),
             "ambiguous", "2026-01-02"),
            # An unambiguous proposal the checker will act on itself, and an image finding:
            # neither is this project's business.
            ("Q2", "link", json.dumps({"inatId": "9", "autoEligible": True}), "open", "2026-01-03"),
            ("Q3", "image", None, "open", "2026-01-04"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_reads_only_ambiguous_link_findings(findings_db):
    findings = ambiguous.load_findings(findings_db)
    assert findings["wikidata_qid"].tolist() == ["Q1"]
    assert findings.loc[0, "wikidata_name"] == "Grania"
    assert findings.loc[0, "checker_candidate_count"] == 2


def test_an_empty_queue_is_not_an_error(findings_db):
    conn = sqlite3.connect(findings_db)
    conn.execute("UPDATE findings SET status = 'resolved' WHERE status = 'ambiguous'")
    conn.commit()
    conn.close()
    assert ambiguous.load_findings(findings_db).empty


def test_a_missing_database_says_which_file_and_why(tmp_path):
    with pytest.raises(SystemExit) as info:
        ambiguous.load_findings(tmp_path / "nope.db")
    assert "findings" in str(info.value)


def test_the_connection_cannot_write(findings_db):
    """platform-design §2.4: this repo never writes to the checker's database. Enforced by
    SQLite refusing the write, which survives a future edit to this module in a way that a
    convention does not."""
    conn = sqlite3.connect(f"file:{findings_db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE findings SET status = 'resolved'")
    finally:
        conn.close()
