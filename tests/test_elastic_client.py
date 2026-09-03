"""Unit tests for elastic_client.paginate: the one search_after loop.

Run with:  .venv\\Scripts\\python.exe -m pytest
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from elastic_client import paginate
from elastic_errors import ElasticFetchError


def _hit(n, with_sort=True):
    h = {"_source": {"n": n}}
    if with_sort:
        h["sort"] = [n]
    return h


class FakeResponse:
    def __init__(self, hits, status=200, text=""):
        self._hits = hits
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        return {"hits": {"hits": self._hits}}


class FakeSession:
    """Yields the queued responses in order; an Exception instance is raised
    instead of returned. Records every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def post(self, endpoint, json=None, headers=None, timeout=None):
        self.bodies.append({"body": json, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build_body(size, search_after):
    body = {"size": size}
    if search_after:
        body["search_after"] = search_after
    return body


def run(session, **kwargs):
    defaults = dict(
        session=session,
        endpoint="http://es/_search",
        headers={},
        page_size=3,
        max_pages=10,
        timeout_sec=5,
    )
    defaults.update(kwargs)
    return paginate(build_body, **defaults)


class TestPagination:
    def test_single_short_page_completes(self):
        session = FakeSession([FakeResponse([_hit(1), _hit(2)])])
        outcome = run(session)
        assert [h["_source"]["n"] for h in outcome.hits] == [1, 2]
        assert outcome.pages == 1
        assert not outcome.truncated
        assert outcome.warning is None

    def test_search_after_threads_between_pages(self):
        session = FakeSession(
            [
                FakeResponse([_hit(1), _hit(2), _hit(3)]),
                FakeResponse([_hit(4)]),
            ]
        )
        outcome = run(session)
        assert len(outcome.hits) == 4
        assert "search_after" not in session.bodies[0]["body"]
        assert session.bodies[1]["body"]["search_after"] == [3]

    def test_empty_first_page_returns_no_hits(self):
        session = FakeSession([FakeResponse([])])
        outcome = run(session)
        assert outcome.hits == []
        assert outcome.pages == 0

    def test_missing_sort_on_full_page_stops_cleanly(self):
        session = FakeSession(
            [FakeResponse([_hit(1), _hit(2), _hit(3, with_sort=False)])]
        )
        outcome = run(session)
        assert len(outcome.hits) == 3
        assert not outcome.truncated

    def test_max_pages_with_full_last_page_flags_truncation(self):
        session = FakeSession(
            [FakeResponse([_hit(n * 3 + 1), _hit(n * 3 + 2), _hit(n * 3 + 3)]) for n in range(2)]
        )
        outcome = run(session, max_pages=2)
        assert len(outcome.hits) == 6
        assert outcome.truncated

    def test_max_hits_budget_shrinks_final_request(self):
        session = FakeSession(
            [
                FakeResponse([_hit(1), _hit(2), _hit(3)]),
                FakeResponse([_hit(4), _hit(5)]),
            ]
        )
        outcome = run(session, max_hits=5)
        assert len(outcome.hits) == 5
        assert session.bodies[1]["body"]["size"] == 2
        assert outcome.truncated

    def test_non_dict_hits_are_dropped(self):
        session = FakeSession([FakeResponse([_hit(1), "junk"])])
        outcome = run(session)
        assert [h["_source"]["n"] for h in outcome.hits] == [1]


class TestRetryLadder:
    def test_timeout_halves_page_size_and_bumps_timeout(self):
        session = FakeSession(
            [
                requests.exceptions.Timeout("t"),
                FakeResponse([_hit(1)]),
            ]
        )
        outcome = run(session, page_size=8, min_page_size=2, timeout_sec=5)
        assert len(outcome.hits) == 1
        assert session.bodies[0]["body"]["size"] == 8
        assert session.bodies[1]["body"]["size"] == 4
        assert session.bodies[1]["timeout"] == 8

    def test_timeout_at_min_size_gets_two_flat_retries(self):
        session = FakeSession(
            [
                requests.exceptions.Timeout("t"),
                requests.exceptions.Timeout("t"),
                FakeResponse([_hit(1)]),
            ]
        )
        outcome = run(session, page_size=2, min_page_size=2)
        assert len(outcome.hits) == 1
        assert all(b["body"]["size"] == 2 for b in session.bodies)

    def test_timeouts_exhausted_raises_with_partial_hits(self):
        session = FakeSession(
            [
                FakeResponse([_hit(1), _hit(2)]),  # full page (size 2)
                requests.exceptions.Timeout("t"),
                requests.exceptions.Timeout("t"),
                requests.exceptions.Timeout("t"),
            ]
        )
        with pytest.raises(ElasticFetchError) as excinfo:
            run(session, page_size=2, min_page_size=2)
        assert len(excinfo.value.items) == 2

    def test_timeouts_exhausted_warns_when_asked(self):
        session = FakeSession(
            [requests.exceptions.Timeout("t")] * 3
        )
        outcome = run(session, page_size=2, min_page_size=2, on_error="warn")
        assert outcome.hits == []
        assert "timeouts" in outcome.warning

    def test_success_resets_flat_retry_budget(self):
        session = FakeSession(
            [
                requests.exceptions.Timeout("t"),
                requests.exceptions.Timeout("t"),
                FakeResponse([_hit(1), _hit(2)]),  # full page, resets retries
                requests.exceptions.Timeout("t"),
                requests.exceptions.Timeout("t"),
                FakeResponse([_hit(3)]),
            ]
        )
        outcome = run(session, page_size=2, min_page_size=2)
        assert len(outcome.hits) == 3

    def test_http_error_raises_immediately_with_response_text(self):
        session = FakeSession(
            [FakeResponse([], status=500, text="mapping boom")]
        )
        with pytest.raises(ElasticFetchError, match="mapping boom"):
            run(session)
        assert len(session.bodies) == 1

    def test_http_error_warn_mode_returns_partial(self):
        session = FakeSession(
            [
                FakeResponse([_hit(1), _hit(2), _hit(3)]),
                FakeResponse([], status=503, text="unavailable"),
            ]
        )
        outcome = run(session, on_error="warn")
        assert len(outcome.hits) == 3
        assert "unavailable" in outcome.warning

    def test_invalid_on_error_rejected(self):
        with pytest.raises(ValueError):
            run(FakeSession([]), on_error="ignore")
