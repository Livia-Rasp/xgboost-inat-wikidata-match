"""Batched SPARQL pull of Wikidata taxon items that already carry P3151 (iNat taxon ID), cached
to parquet. See docs/inat-wikidata-match-spec.md §0 (properties) and §7 milestone 2.

Mirrors the SPARQL client pattern in wikidata-inat-checker's lib/utils.js: endpoint, descriptive
User-Agent, WDQS's generous timeout, retry/backoff on transient errors, and POSTed batched
VALUES queries instead of asking WDQS to scan/join the full (856k-row) taxon set at once.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd
import requests

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

try:
    _VERSION = version("xgboost-inat-wikidata-match")
except PackageNotFoundError:
    _VERSION = "0.0.0"

# Wikimedia blocks anonymous clients; the contact URL is the part that must never be dropped.
USER_AGENT = (
    f"xgboost-inat-wikidata-match/{_VERSION} "
    "(https://github.com/Livia-Rasp/xgboost-inat-wikidata-match)"
)
HEADERS = {"User-Agent": USER_AGENT}

# WDQS's own query limit is 60s and it has been slower through 2026; a shorter client timeout
# would abandon queries the service intends to answer.
SPARQL_TIMEOUT_S = 90

RETRYABLE_STATUS = {429, 502, 503, 504}

DEFAULT_TARGET_SIZE = 60_000
# Empirically timed against the real endpoint: 500 QIDs ~1.7s, 2000 QIDs ~4.2s, both well
# under the 90s timeout. 2000 keeps the request count down (~30 for 60k) without measurable risk.
ATTRIBUTE_BATCH_SIZE = 2000

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "wikidata_taxa.parquet"
DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "wikidata_taxa.manifest.json"
)

COLUMNS = [
    "qid",
    "inat_id",
    "name",
    "rank_qid",
    "parent_qid",
    "parent_name",
    "iucn_qid",
    "sitelinks",
    "statements",
    "has_commons_cat",
    "p3151_has_reference",
    "synonym_names",
    "basionym_names",
]


@dataclass
class PullResult:
    taxa: pd.DataFrame
    cache_hit: bool


class RateLimiter:
    """Enforces a minimum gap between calls. One instance per caller, so limits don't bleed
    across unrelated code, matching lib/utils.js's createRateLimiter()."""

    def __init__(self, interval_s: float = 1.0):
        self._interval = interval_s
        self._next_slot = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self._interval
        if slot > now:
            time.sleep(slot - now)


def _retry_delay(status: int, retries_left: int) -> float:
    return 30.0 if status == 429 else (4 - retries_left) * 3.0


def _fetch_with_retry(do_request, retries: int = 3, label: str = "SPARQL") -> requests.Response:
    while True:
        resp = do_request()
        if resp.status_code in RETRYABLE_STATUS and retries > 0:
            delay = _retry_delay(resp.status_code, retries)
            print(f"{label} HTTP {resp.status_code}, retrying in {delay:.0f}s...")
            time.sleep(delay)
            retries -= 1
            continue
        resp.raise_for_status()
        return resp


def _parse_sparql_tsv(text: str) -> list[dict]:
    """Parse a SPARQL TSV response. URIs come back as the bare URI string; literals have
    surrounding quotes stripped. Mirrors lib/utils.js's parseSparqlTSV()."""
    text = text.lstrip("\ufeff")
    lines = text.split("\n")
    if len(lines) < 2:
        return []
    headers = [h.rstrip("\r").lstrip("?") for h in lines[0].split("\t")]
    rows = []
    for line in lines[1:]:
        line = line.rstrip("\r")
        if not line.strip():
            continue
        cells = line.split("\t")
        row: dict = {}
        for i, header in enumerate(headers):
            cell = cells[i].strip() if i < len(cells) else ""
            if not cell:
                continue
            if cell.startswith("<"):
                row[header] = cell[1:-1]
            elif cell.startswith('"'):
                last = cell.rfind('"')
                row[header] = cell[1:last].replace('\\"', '"').replace("\\\\", "\\")
            else:
                row[header] = cell
        rows.append(row)
    return rows


def _sparql_get_tsv(query: str, retries: int = 3) -> list[dict]:
    resp = _fetch_with_retry(
        lambda: requests.get(
            SPARQL_ENDPOINT,
            params={"query": query},
            headers={**HEADERS, "Accept": "text/tab-separated-values"},
            timeout=SPARQL_TIMEOUT_S,
        ),
        retries,
    )
    return _parse_sparql_tsv(resp.text)


def _sparql_post_tsv(query: str, retries: int = 3) -> list[dict]:
    resp = _fetch_with_retry(
        lambda: requests.post(
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={**HEADERS, "Accept": "text/tab-separated-values"},
            timeout=SPARQL_TIMEOUT_S,
        ),
        retries,
    )
    return _parse_sparql_tsv(resp.text)


def _qid_from_uri(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def fetch_p3151_qids(target_size: int = DEFAULT_TARGET_SIZE) -> list[str]:
    """Phase 1: enumerate QIDs of taxa (P31=Q16521) that carry P3151, LIMIT-capped so WDQS can
    short-circuit instead of scanning+joining the full ~856k-row population."""
    query = f"""SELECT ?item WHERE {{
  ?item wdt:P31 wd:Q16521 .
  ?item wdt:P3151 ?inatId .
}} LIMIT {target_size}"""
    rows = _sparql_get_tsv(query)
    return [_qid_from_uri(r["item"]) for r in rows if r.get("item")]


_ATTRIBUTE_RATE_LIMITER = RateLimiter(0.5)


def _fetch_attributes_batch(qids: list[str]) -> list[dict]:
    """Phase 2: pull the full attribute set for one VALUES-batch of QIDs.

    Uses the full p:/ps: statement pattern (not the wdt: truthy shortcut) for P3151 specifically,
    constrained to wikibase:BestRank to match wdt:'s semantics (excludes deprecated statements),
    so the statement node is available to check whether it carries a reference — spec §3's
    label-noise mitigation needs that signal, and it's free to capture here rather than requiring
    a second pull over the same QIDs later.

    P1420 (synonym) and P566 (basionym) are multi-valued and OPTIONAL, so an item with several
    synonyms comes back as several rows — resolved by _aggregate_batch_rows(), not here.
    """
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""SELECT ?item ?inatId ?name ?rank ?parent ?parentName ?iucn ?sitelinks ?statements ?commonsSitelink ?p3151Ref ?synonymName ?basionymName WHERE {{
  VALUES ?item {{ {values} }}
  ?item p:P3151 ?p3151Statement .
  ?p3151Statement a wikibase:BestRank ; ps:P3151 ?inatId .
  OPTIONAL {{ ?p3151Statement prov:wasDerivedFrom ?p3151Ref . }}
  OPTIONAL {{ ?item wdt:P225 ?name . }}
  OPTIONAL {{ ?item wdt:P105 ?rank . }}
  OPTIONAL {{ ?item wdt:P171 ?parent . OPTIONAL {{ ?parent wdt:P225 ?parentName . }} }}
  OPTIONAL {{ ?item wdt:P141 ?iucn . }}
  ?item wikibase:sitelinks ?sitelinks .
  ?item wikibase:statements ?statements .
  OPTIONAL {{
    ?commonsSitelink schema:about ?item ; schema:isPartOf <https://commons.wikimedia.org/> .
    FILTER(CONTAINS(STR(?commonsSitelink), "/wiki/Category:"))
  }}
  OPTIONAL {{ ?item wdt:P1420 ?synonym . OPTIONAL {{ ?synonym wdt:P225 ?synonymName . }} }}
  OPTIONAL {{ ?item wdt:P566 ?basionym . OPTIONAL {{ ?basionym wdt:P225 ?basionymName . }} }}
}}"""
    _ATTRIBUTE_RATE_LIMITER.wait()
    return _sparql_post_tsv(query)


def _aggregate_batch_rows(rows: list[dict]) -> list[dict]:
    """Group a batch's raw TSV rows by item, since multi-valued P171/P1420/P566 OPTIONALs can
    produce several rows per item. Scalar fields take the first non-null value seen; synonym and
    basionym names are collected into a deduplicated, sorted list per item."""
    by_item: dict[str, dict] = {}
    for r in rows:
        item = r.get("item")
        if not item:
            continue
        group = by_item.setdefault(item, {"row": {}, "synonyms": set(), "basionyms": set()})
        for key, value in r.items():
            if key in ("synonymName", "basionymName"):
                continue
            if value and key not in group["row"]:
                group["row"][key] = value
        if r.get("synonymName"):
            group["synonyms"].add(r["synonymName"])
        if r.get("basionymName"):
            group["basionyms"].add(r["basionymName"])

    records = []
    for item, group in by_item.items():
        r = group["row"]
        records.append(
            {
                "qid": _qid_from_uri(item),
                "inat_id": r.get("inatId"),
                "name": r.get("name"),
                "rank_qid": _qid_from_uri(r["rank"]) if r.get("rank") else None,
                "parent_qid": _qid_from_uri(r["parent"]) if r.get("parent") else None,
                "parent_name": r.get("parentName"),
                "iucn_qid": _qid_from_uri(r["iucn"]) if r.get("iucn") else None,
                "sitelinks": int(r["sitelinks"]) if r.get("sitelinks") else 0,
                "statements": int(r["statements"]) if r.get("statements") else 0,
                "has_commons_cat": bool(r.get("commonsSitelink")),
                "p3151_has_reference": bool(r.get("p3151Ref")),
                "synonym_names": sorted(group["synonyms"]),
                "basionym_names": sorted(group["basionyms"]),
            }
        )
    return records


def _manifest_matches(manifest_path: Path, target_size: int) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("target_size") == target_size and manifest.get("columns") == COLUMNS


def build_pull_cache(
    target_size: int = DEFAULT_TARGET_SIZE,
    cache_path: Path = DEFAULT_CACHE_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    force_refresh: bool = False,
) -> PullResult:
    """Return the cached pull if the manifest matches the requested target size and column set;
    otherwise pull fresh from Wikidata and (re)write the cache. No time-based staleness check —
    unlike milestone 1's taxa.db mtime comparison, there's no local source file to diff against,
    and silently re-hitting a shared public endpoint on a staleness guess isn't the right
    default. Pass force_refresh=True for a deliberate re-pull."""
    if not force_refresh and cache_path.exists() and _manifest_matches(manifest_path, target_size):
        return PullResult(pd.read_parquet(cache_path), cache_hit=True)

    qids = fetch_p3151_qids(target_size)

    records: list[dict] = []
    seen: set[str] = set()
    for batch in _chunked(qids, ATTRIBUTE_BATCH_SIZE):
        for record in _aggregate_batch_rows(_fetch_attributes_batch(batch)):
            qid = record["qid"]
            if qid and qid not in seen:
                seen.add(qid)
                records.append(record)

    df = pd.DataFrame.from_records(records, columns=COLUMNS)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    manifest_path.write_text(
        json.dumps(
            {
                "pulled_at": datetime.now(timezone.utc).isoformat(),
                "target_size": target_size,
                "columns": COLUMNS,
                "endpoint": SPARQL_ENDPOINT,
            },
            indent=2,
        )
    )
    return PullResult(df, cache_hit=False)


if __name__ == "__main__":
    result = build_pull_cache()
    kind = "cache hit, no network" if result.cache_hit else "fresh pull"
    print(f"{len(result.taxa):,} Wikidata taxa with P3151 ({kind})")
    print(result.taxa.head(3).to_string())
