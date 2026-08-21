"""Parse a fresh links-ambiguous.html (from wikidata-inat-checker's `npm run links`), sample a
manageable subset, and produce a labeling kit: a trimmed HTML (same review UI, scoped to the
sample) + a CSV template for recording answers. See gold/README.md for the full reproduction
and labeling workflow this feeds into.

Lives at the repo root, not src/ or a new scripts/ dir — matching the convention spec's own
milestone 9 implies for this kind of one-off "read links-ambiguous.html, do a thing" tooling
(score_ambiguous.py), not part of the core src/ pipeline.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

SIBLING_REPO = Path.home() / "repos" / "wikidata-inat-checker"
SOURCE_HTML_PATH = SIBLING_REPO / "output" / "links-ambiguous.html"

GOLD_DIR = Path(__file__).resolve().parent / "gold"
SAMPLE_HTML_PATH = GOLD_DIR / "links-ambiguous-sample.html"
TEMPLATE_CSV_PATH = GOLD_DIR / "labeling_template.csv"
LOOKUP_SQLITE_PATH = Path(__file__).resolve().parent / "data" / "lookup.sqlite"

SAMPLE_SIZE = 500
RANDOM_SEED = 42


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


def lookup_inat_names(taxon_ids: set[str]) -> dict[str, str]:
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


def write_trimmed_html(soup: BeautifulSoup, sampled_qids: set[str], out_path: Path) -> None:
    """Same page (styles, scripts, table structure) with only the sampled items' row-groups
    kept — everything else about the reviewing UI (checkboxes, tree-pair comparison,
    QuickStatements copy) works exactly as in the original."""
    current_qid = None
    for tr in soup.find_all("tr"):
        row_id = tr.get("id", "")
        classes = tr.get("class", [])
        if row_id.startswith("row-"):
            current_qid = row_id[len("row-") :]
            if current_qid not in sampled_qids:
                tr.decompose()
        elif "candidate-row" in classes:
            qid = tr.get("data-qid", current_qid)
            if qid not in sampled_qids:
                tr.decompose()
    out_path.write_text(str(soup))


def write_template_csv(groups: list[dict], inat_names: dict[str, str], out_path: Path) -> None:
    import csv

    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "wikidata_qid",
                "wikidata_name",
                "wikidata_url",
                "candidate_inat_ids",
                "candidate_inat_names",
                "correct_inat_taxon_id",
                "notes",
            ]
        )
        for g in groups:
            ids = [c["inat_taxon_id"] for c in g["candidates"]]
            names = [inat_names.get(tid, "?") for tid in ids]
            writer.writerow(
                [
                    g["wikidata_qid"],
                    g["wikidata_name"],
                    g["wikidata_url"],
                    ";".join(ids),
                    ";".join(names),
                    "",
                    "",
                ]
            )


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

    rng = random.Random(RANDOM_SEED)
    sample = rng.sample(groups, min(SAMPLE_SIZE, len(groups)))
    sample.sort(key=lambda g: g["wikidata_name"].lower())
    sampled_qids = {g["wikidata_qid"] for g in sample}
    print(f"sampled {len(sample):,} items (seed={RANDOM_SEED}), sorted by wikidata_name")

    all_candidate_ids = {c["inat_taxon_id"] for g in sample for c in g["candidates"]}
    inat_names = lookup_inat_names(all_candidate_ids)
    print(f"resolved names for {len(inat_names):,}/{len(all_candidate_ids):,} candidate taxa")

    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    write_trimmed_html(BeautifulSoup(html, "html.parser"), sampled_qids, SAMPLE_HTML_PATH)
    write_template_csv(sample, inat_names, TEMPLATE_CSV_PATH)
    print(f"wrote {SAMPLE_HTML_PATH}")
    print(f"wrote {TEMPLATE_CSV_PATH}")


if __name__ == "__main__":
    main()
