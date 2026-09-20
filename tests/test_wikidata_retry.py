"""The WDQS client's failure handling. Spec §7 milestone 16.

Every failure mode here has happened against the real endpoint (CLAUDE.md, milestones 4 and 7),
and every one of them used to be either fatal on first sight or silent. The orchestrator can only
retry what surfaces as a transient exception, so these tests pin both halves: what is retried
in-process, and what is raised once the in-process budget is spent. No network — requests are
faked, and time.sleep is patched out.
"""

from __future__ import annotations

import pytest
import requests

from src import wikidata


class FakeResponse:
    def __init__(self, status: int = 200, text: str = "?item\n"):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def scripted(*outcomes):
    """A do_request() that plays back responses or raises exceptions, in order."""
    calls = iter(outcomes)

    def do_request():
        outcome = next(calls)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return do_request


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wikidata.time, "sleep", lambda _s: None)


# -- connection failures are retried, not fatal ------------------------------------------------


def test_timeout_is_retried_in_process():
    """A single hung connection used to propagate straight out of the pull and discard every
    batch already fetched."""
    ok = FakeResponse()
    do_request = scripted(requests.Timeout("hung"), requests.ConnectionError("reset"), ok)
    assert wikidata._fetch_with_retry(do_request, retries=3) is ok


def test_timeout_is_raised_once_the_budget_is_spent():
    do_request = scripted(*[requests.Timeout("hung")] * 3)
    with pytest.raises(requests.Timeout) as info:
        wikidata._fetch_with_retry(do_request, retries=2)
    assert wikidata.is_transient(info.value)


# -- truncated bodies are rejected, not parsed -------------------------------------------------


def test_truncated_tsv_is_retried():
    good = FakeResponse(text="?item\n<http://www.wikidata.org/entity/Q1>\n")
    cut = FakeResponse(text="?item\n<http://www.wikidata.org/entity/Q1>\n<http://www.wiki")
    do_request = scripted(cut, good)
    assert wikidata._fetch_with_retry(do_request, validate=wikidata._tsv_truncation) is good


def test_truncated_tsv_raises_transient_when_it_persists():
    cut = FakeResponse(text="?item\n<http://www.wiki")
    with pytest.raises(wikidata.TransientSourceError):
        wikidata._fetch_with_retry(scripted(cut, cut), retries=1, validate=wikidata._tsv_truncation)


def test_empty_result_is_not_mistaken_for_truncation():
    """WDQS sends the header line alone, newline-terminated, for zero rows."""
    assert wikidata._tsv_truncation(FakeResponse(text="?item\n")) is None


# -- low ancestor coverage is raised, not cached -----------------------------------------------


def test_low_ancestor_coverage_raises_after_retries(monkeypatch):
    """Used to return the last low-coverage rows, which build_ancestor_chains() then cached as a
    complete pull."""
    qids = [f"Q{i}" for i in range(10)]
    low = [{"item": "http://www.wikidata.org/entity/Q0", "ancestor": "x"}]
    calls = []
    monkeypatch.setattr(wikidata, "_fetch_ancestor_batch_raw", lambda q: calls.append(q) or low)

    with pytest.raises(wikidata.TransientSourceError):
        wikidata._fetch_ancestor_batch(qids)
    assert len(calls) == 1 + wikidata.ANCESTOR_COVERAGE_RETRIES


def test_ancestor_coverage_recovers_on_retry(monkeypatch):
    qids = [f"Q{i}" for i in range(4)]
    low = [{"item": "http://www.wikidata.org/entity/Q0"}]
    full = [{"item": f"http://www.wikidata.org/entity/{q}"} for q in qids]
    responses = iter([low, full])
    monkeypatch.setattr(wikidata, "_fetch_ancestor_batch_raw", lambda q: next(responses))
    assert wikidata._fetch_ancestor_batch(qids) == full


# -- the classification the DAGs retry on ------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.Timeout(), True),
        (requests.ConnectionError(), True),
        (requests.exceptions.ChunkedEncodingError(), True),
        (wikidata.TransientSourceError("cut"), True),
        (requests.HTTPError(response=FakeResponse(503)), True),
        (requests.HTTPError(response=FakeResponse(429)), True),
        (requests.HTTPError(response=FakeResponse(400)), False),
        (KeyError("item"), False),
        (ValueError("bad"), False),
    ],
)
def test_is_transient(exc, expected):
    assert wikidata.is_transient(exc) is expected
