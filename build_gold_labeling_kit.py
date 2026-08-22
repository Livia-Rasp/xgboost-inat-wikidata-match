"""Parse a fresh links-ambiguous.html (from wikidata-inat-checker's `npm run links`), append one
blank row per item not already in gold/labeling_filled.csv directly onto that file, and write a
trimmed review HTML scoped to just those new items. No target sample size — every run picks up
whatever's new since the last one; leaving rows blank in the CSV is how you decide not to label
everything. There used to be a separate labeling_template.csv you'd fill in and "save as"
labeling_filled.csv — dropped as pure duplication once this script started carrying existing
answers forward unchanged: appending straight onto the one file you actually edit is exactly as
safe (existing rows are never read back, let alone rewritten) and removes a manual copy step.
See gold/README.md for the full reproduction and labeling workflow this feeds into.

Lives at the repo root, not src/ or a new scripts/ dir — matching the convention spec's own
milestone 9 implies for this kind of one-off "read links-ambiguous.html, do a thing" tooling
(score_ambiguous.py), not part of the core src/ pipeline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

SIBLING_REPO = Path.home() / "repos" / "wikidata-inat-checker"
SOURCE_HTML_PATH = SIBLING_REPO / "output" / "links-ambiguous.html"

GOLD_DIR = Path(__file__).resolve().parent / "gold"
SAMPLE_HTML_PATH = GOLD_DIR / "links-ambiguous-sample.html"
FILLED_CSV_PATH = GOLD_DIR / "labeling_filled.csv"
LOOKUP_SQLITE_PATH = Path(__file__).resolve().parent / "data" / "lookup.sqlite"

CSV_FIELDNAMES = [
    "wikidata_qid",
    "wikidata_name",
    "wikidata_url",
    "candidate_inat_ids",
    "candidate_inat_names",
    "correct_inat_taxon_id",
    "notes",
]


def parse_ambiguous_groups(soup: BeautifulSoup) -> list[dict]:
    """One entry per ambiguous WD item: qid, name, wd url, and its candidate iNat taxa (id, url,
    rank — names aren't in the HTML at all, just ids/ranks; looked up separately)."""
    groups: list[dict] = []
    current: dict | None = None
    for tr in soup.find_all("tr"):
        row_id = tr.get("id", "")
        classes = tr.get("class", [])
        if row_id.startswith("row-"):
            qid = row_id[len("row-") :]
            wd_link = tr.select_one("td.wd-col a")
            taxon_col = tr.select_one("td.taxon-col")
            current = {
                "wikidata_qid": qid,
                "wikidata_name": taxon_col.get_text(strip=True) if taxon_col else "",
                "wikidata_url": wd_link["href"] if wd_link else f"https://www.wikidata.org/wiki/{qid}",
                "candidates": [],
            }
            cand = _extract_candidate(tr)
            if cand:
                current["candidates"].append(cand)
            groups.append(current)
        elif "candidate-row" in classes and current is not None:
            cand = _extract_candidate(tr)
            if cand:
                current["candidates"].append(cand)
    return groups


def _extract_candidate(tr) -> dict | None:
    inat_cell = tr.select_one("td.inat-col")
    if not inat_cell:
        return None
    link = inat_cell.select_one("a")
    if not link:
        return None
    rank_badge = inat_cell.select_one(".rank-badge")
    return {
        "inat_taxon_id": link.get_text(strip=True),
        "inat_url": link.get("href", ""),
        "rank": rank_badge.get_text(strip=True) if rank_badge else "",
    }


def load_existing_rows(path: Path) -> list[dict[str, str]]:
    """Every row already shown to Livia (answered or deliberately left blank/skipped) — read
    verbatim so a re-run never disturbs an existing answer."""
    import csv

    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def lookup_inat_names(taxon_ids: set[str]) -> dict[str, str]:
    if not taxon_ids:
        return {}
    conn = sqlite3.connect(f"file:{LOOKUP_SQLITE_PATH}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(taxon_ids))
        rows = conn.execute(
            f"SELECT taxon_id, name FROM taxa_normalized WHERE taxon_id IN ({placeholders})",
            list(taxon_ids),
        ).fetchall()
        return dict(rows)
    finally:
        conn.close()


def write_trimmed_html(soup: BeautifulSoup, keep_qids: set[str], out_path: Path) -> None:
    """Same page (styles, scripts, table structure) with only the new items' row-groups kept —
    everything else about the reviewing UI (checkboxes, tree-pair comparison, QuickStatements
    copy) works exactly as in the original.

    Two passes, not one: this HTML's table markup nests candidate rows deeply enough that
    bs4's decompose() (which clears the whole next_element chain from the decomposed tag
    onward) can reach past that tag's own subtree into later siblings — corrupting rows this
    loop hasn't inspected yet if decomposed during forward iteration. Deciding what to remove
    first, then decomposing in reverse document order, means every removal only ever touches
    elements already inspected (or earlier in the tree), so the corruption can't matter."""
    current_qid = None
    to_remove = []
    for tr in soup.find_all("tr"):
        row_id = tr.get("id", "")
        classes = tr.get("class", [])
        if row_id.startswith("row-"):
            current_qid = row_id[len("row-") :]
            if current_qid not in keep_qids:
                to_remove.append(tr)
        elif "candidate-row" in classes:
            qid = tr.get("data-qid", current_qid)
            if qid not in keep_qids:
                to_remove.append(tr)
    for tr in reversed(to_remove):
        tr.decompose()
    out_path.write_text(str(soup))


def append_new_rows(new_groups: list[dict], inat_names: dict[str, str], out_path: Path) -> None:
    """Appends one blank-answer row per new item straight onto out_path (writing the header
    first if the file doesn't exist yet). Only ever appends — never reads, reorders, or rewrites
    an existing row — so this is safe to point directly at the tracked, hand-edited answer file
    instead of a separate scratch template that would need a manual "save as" to promote."""
    import csv

    file_exists = out_path.exists()
    with out_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        for g in new_groups:
            ids = [c["inat_taxon_id"] for c in g["candidates"]]
            names = [inat_names.get(tid, "?") for tid in ids]
            writer.writerow(
                {
                    "wikidata_qid": g["wikidata_qid"],
                    "wikidata_name": g["wikidata_name"],
                    "wikidata_url": g["wikidata_url"],
                    "candidate_inat_ids": ";".join(ids),
                    "candidate_inat_names": ";".join(names),
                    "correct_inat_taxon_id": "",
                    "notes": "",
                }
            )


def next_sample_html_path() -> Path:
    """Never overwrite a previous batch's sample HTML — Livia may still be working through it
    side-by-side with labeling_filled.csv. The first batch keeps the plain
    `links-ambiguous-sample.html` name; every later batch gets its own `-N` suffix instead of
    clobbering an in-progress file."""
    if not SAMPLE_HTML_PATH.exists():
        return SAMPLE_HTML_PATH
    n = 2
    while (candidate := GOLD_DIR / f"links-ambiguous-sample-{n}.html").exists():
        n += 1
    return candidate


def main() -> None:
    if not SOURCE_HTML_PATH.exists():
        raise SystemExit(
            f"{SOURCE_HTML_PATH} not found — run `npm run links -- --limit 150000` in "
            f"{SIBLING_REPO} first (see gold/README.md)."
        )

    html = SOURCE_HTML_PATH.read_text()
    soup = BeautifulSoup(html, "html.parser")
    groups = parse_ambiguous_groups(soup)
    print(f"{len(groups):,} ambiguous WD items found in {SOURCE_HTML_PATH.name}")

    existing_rows = load_existing_rows(FILLED_CSV_PATH)
    existing_qids = {row["wikidata_qid"] for row in existing_rows}
    if existing_rows:
        print(f"{len(existing_rows):,} items already in {FILLED_CSV_PATH.name} — excluded")

    new_items = [g for g in groups if g["wikidata_qid"] not in existing_qids]
    new_items.sort(key=lambda g: g["wikidata_name"].lower())
    new_qids = {g["wikidata_qid"] for g in new_items}
    print(f"{len(new_items):,} new items (every ambiguous item not already in {FILLED_CSV_PATH.name}), sorted by wikidata_name")

    if not new_items:
        print("nothing new to add")
        return

    all_candidate_ids = {c["inat_taxon_id"] for g in new_items for c in g["candidates"]}
    inat_names = lookup_inat_names(all_candidate_ids)
    print(f"resolved names for {len(inat_names):,}/{len(all_candidate_ids):,} candidate taxa")

    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    append_new_rows(new_items, inat_names, FILLED_CSV_PATH)
    print(f"appended {len(new_items):,} rows to {FILLED_CSV_PATH} ({len(existing_rows) + len(new_items):,} total rows)")

    sample_html_path = next_sample_html_path()
    write_trimmed_html(BeautifulSoup(html, "html.parser"), new_qids, sample_html_path)
    print(f"wrote {sample_html_path} ({len(new_items):,} new items)")


if __name__ == "__main__":
    main()
