"""Deterministic coverage of the shared HTTP layer (paper_search_mcp.http_limits)."""
import io
import os
import sqlite3
import threading
import time

import pytest
import requests
from requests.adapters import HTTPAdapter

from paper_search_mcp import http_limits
from paper_search_mcp.academic_platforms.acm import ACMSearcher
from paper_search_mcp.academic_platforms.arxiv import ArxivSearcher
from paper_search_mcp.academic_platforms.base_search import BASESearcher
from paper_search_mcp.academic_platforms.biorxiv import BioRxivSearcher
from paper_search_mcp.academic_platforms.chemrxiv import ChemRxivSearcher
from paper_search_mcp.academic_platforms.citeseerx import CiteSeerXSearcher
from paper_search_mcp.academic_platforms.core import CORESearcher
from paper_search_mcp.academic_platforms.crossref import CrossRefSearcher
from paper_search_mcp.academic_platforms.dblp import DBLPSearcher
from paper_search_mcp.academic_platforms.doaj import DOAJSearcher
from paper_search_mcp.academic_platforms.europepmc import EuropePMCSearcher
from paper_search_mcp.academic_platforms.google_scholar import GoogleScholarSearcher
from paper_search_mcp.academic_platforms.hal import HALSearcher
from paper_search_mcp.academic_platforms.iacr import IACRSearcher
from paper_search_mcp.academic_platforms.ieee import IEEESearcher
from paper_search_mcp.academic_platforms.medrxiv import MedRxivSearcher
from paper_search_mcp.academic_platforms.openaire import OpenAiresearcher
from paper_search_mcp.academic_platforms.openalex import OpenAlexSearcher
from paper_search_mcp.academic_platforms.openreview import OpenReviewSearcher
from paper_search_mcp.academic_platforms.pmc import PMCSearcher
from paper_search_mcp.academic_platforms.scopus import ScopusSearcher
from paper_search_mcp.academic_platforms.semantic import SemanticSearcher
from paper_search_mcp.academic_platforms.ssrn import SSRNSearcher
from paper_search_mcp.academic_platforms.unpaywall import UnpaywallResolver
from paper_search_mcp.academic_platforms.wos import WebOfScienceSearcher
from paper_search_mcp.academic_platforms.zenodo import ZenodoSearcher
from paper_search_mcp.http_limits import ServiceLimit

CONNECTORS = [
    ACMSearcher, ArxivSearcher, BASESearcher, BioRxivSearcher, ChemRxivSearcher,
    CiteSeerXSearcher, CORESearcher, CrossRefSearcher, DBLPSearcher, DOAJSearcher,
    EuropePMCSearcher, GoogleScholarSearcher, HALSearcher, IACRSearcher, IEEESearcher,
    MedRxivSearcher, OpenAiresearcher, OpenAlexSearcher, OpenReviewSearcher, PMCSearcher,
    ScopusSearcher, SemanticSearcher, SSRNSearcher, UnpaywallResolver, WebOfScienceSearcher,
    ZenodoSearcher,
]


class FakeClock:
    """Wall time (now) can be stepped; monotonic time only advances with sleeps."""

    def __init__(self):
        self.now = 1_000_000.0
        self.elapsed = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        self.elapsed += seconds


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(http_limits, "SERVICE_LIMITS", dict(http_limits.SERVICE_LIMITS))
    monkeypatch.setattr(http_limits, "_adapter_factory", None)
    monkeypatch.setattr(http_limits, "_connection", None)
    monkeypatch.delenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", raising=False)
    monkeypatch.delenv("RATE_LIMIT_DIR", raising=False)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(http_limits, "_clock", fake.time)
    monkeypatch.setattr(http_limits, "_monotonic", fake.monotonic)
    monkeypatch.setattr(http_limits, "_sleep", fake.sleep)
    return fake


def make_response(status):
    response = requests.Response()
    response.raw = io.BytesIO(b"")
    response.status_code = status
    return response


@pytest.mark.parametrize(("url", "service"), [
    ("https://export.arxiv.org/api/query?search_query=all:x", "arxiv"),
    ("https://arxiv.org/pdf/2401.12345.pdf", "arxiv"),
    ("https://www.arxiv.org/pdf/2401.12345.pdf", "arxiv"),
    ("https://API.OPENALEX.ORG/works", "openalex"),
])
def test_limit_for_listed_hosts(url, service):
    assert http_limits.RateLimitedAdapter().limit_for(url).service == service


def test_unlisted_host_is_not_limited():
    assert http_limits.RateLimitedAdapter().limit_for("https://example.org/") is None


def test_register_service_limit_adds_and_overrides_hosts():
    custom = ServiceLimit("custom", 1, 5.0)
    http_limits.register_service_limit(("api.crossref.org", "Custom.Example"), custom)
    adapter = http_limits.RateLimitedAdapter()
    assert adapter.limit_for("https://api.crossref.org/works") is custom
    assert adapter.limit_for("https://custom.example/x") is custom


@pytest.mark.parametrize("kwargs", [
    {"count": 0, "seconds": 1.0},
    {"count": 1, "seconds": 0},
    {"count": 1, "seconds": 1.0, "credentialed_count": 0, "credential": "X-Key"},
    {"count": 1, "seconds": 1.0, "credentialed_count": 2},
    {"count": 1, "seconds": 1.0, "credential": "X-Key"},
])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError, match="invalid rate limit"):
        ServiceLimit("bad", **kwargs)


def test_credentialed_count_applies_to_credentialed_requests(clock):
    keyed = ServiceLimit("svc", 1, 3.0, credentialed_count=2, credential="X-Key")
    http_limits.acquire(keyed, credentialed=True)
    http_limits.acquire(keyed, credentialed=True)
    assert clock.sleeps == []
    http_limits.acquire(keyed)
    http_limits.acquire(keyed)
    assert clock.sleeps == [3.0]


def test_acquire_spaces_requests(clock):
    for _ in range(3):
        http_limits.acquire(ServiceLimit("svc", 1, 3.0))
    assert clock.sleeps == [3.0, 3.0]


def test_acquire_allows_count_per_window(clock):
    for _ in range(3):
        http_limits.acquire(ServiceLimit("svc", 2, 1.0))
    assert clock.sleeps == [1.0]


def test_services_are_limited_independently(clock):
    http_limits.acquire(ServiceLimit("one", 1, 3.0))
    http_limits.acquire(ServiceLimit("two", 1, 3.0))
    assert clock.sleeps == []


def test_slot_beyond_max_wait_fails_fast(clock):
    slow = ServiceLimit("slow", 1, 60.0)
    http_limits.acquire(slow)
    with pytest.raises(http_limits.RateLimited, match="slow: next request slot is 60 s away"):
        http_limits.acquire(slow, max_wait=10.0)
    assert clock.sleeps == []
    assert issubclass(http_limits.RateLimited, requests.RequestException)


def test_max_wait_spans_slots_lost_to_other_waiters(clock, monkeypatch):
    paced = ServiceLimit("svc", 1, 3.0)
    http_limits.acquire(paced)

    def sleep_while_another_waiter_takes_the_slot(seconds):
        clock.sleep(seconds)
        http_limits.acquire(paced)

    monkeypatch.setattr(http_limits, "_sleep", sleep_while_another_waiter_takes_the_slot)
    with pytest.raises(http_limits.RateLimited):
        http_limits.acquire(paced, max_wait=4.0)
    assert clock.sleeps == [3.0]


def test_backward_clock_step_while_waiting_does_not_extend_max_wait(clock, monkeypatch):
    paced = ServiceLimit("svc", 1, 3.0)
    http_limits.acquire(paced)

    def sleep_while_another_waiter_takes_the_slot_then_clock_steps_back(seconds):
        clock.sleep(seconds)
        http_limits.acquire(paced)
        clock.now -= 3600

    monkeypatch.setattr(http_limits, "_sleep", sleep_while_another_waiter_takes_the_slot_then_clock_steps_back)
    with pytest.raises(http_limits.RateLimited):
        http_limits.acquire(paced, max_wait=4.0)
    assert clock.sleeps == [3.0]


def test_backward_clock_step_waits_at_most_one_window(clock):
    paced = ServiceLimit("svc", 1, 3.0)
    http_limits.acquire(paced)
    clock.now -= 3600
    http_limits.acquire(paced)
    assert clock.sleeps == [3.0]


def test_limiter_ignores_patched_time_sleep(monkeypatch):
    # Connector tests patch time.sleep globally; pacing must neither skip its wait nor spin.
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("limiter used time.sleep"))
    short = ServiceLimit("svc", 1, 0.05)
    http_limits.acquire(short)
    start = time.monotonic()
    http_limits.acquire(short)
    assert time.monotonic() - start >= 0.04


def test_shared_directory_is_shared_between_connections(monkeypatch, tmp_path, clock):
    # A second connection to the same file stands in for another process.
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(tmp_path / "limits"))
    paced = ServiceLimit("svc", 1, 3.0)
    http_limits.acquire(paced)
    monkeypatch.setattr(http_limits, "_connection", None)
    http_limits.acquire(paced)
    assert clock.sleeps == [3.0]
    assert (tmp_path / "limits" / "rate_limits.sqlite").is_file()


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.DatabaseError("file is not a database"),
])
def test_unavailable_store_raises_rate_limited(monkeypatch, error):
    class FailingConnection:
        in_transaction = False

        def execute(self, sql, *args):
            raise error

    monkeypatch.setattr(http_limits, "_connection", FailingConnection())
    with pytest.raises(http_limits.RateLimited, match="svc: rate-limit store unavailable"):
        http_limits.acquire(ServiceLimit("svc", 1, 3.0))


def test_shared_store_locked_by_another_process_still_starts(monkeypatch, tmp_path):
    directory = tmp_path / "limits"
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(directory))
    http_limits.make_session()
    monkeypatch.setattr(http_limits, "_connection", None)
    other = sqlite3.connect(directory / "rate_limits.sqlite", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        http_limits.make_session()
        assert time.monotonic() - start < 1
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_threads_queued_on_a_locked_store_each_fail_within_max_wait(monkeypatch, tmp_path):
    directory = tmp_path / "limits"
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(directory))
    http_limits.make_session()
    other = sqlite3.connect(directory / "rate_limits.sqlite", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    elapsed = []

    def wait_for_slot():
        start = time.monotonic()
        with pytest.raises(http_limits.RateLimited):
            http_limits.acquire(ServiceLimit("svc", 1, 1.0), max_wait=0.3)
        elapsed.append(time.monotonic() - start)

    threads = [threading.Thread(target=wait_for_slot) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert len(elapsed) == 3
    assert max(elapsed) < 0.9


def test_make_session_does_not_wait_for_the_limiter_once_connected():
    http_limits.make_session()
    done = threading.Event()
    with http_limits._connection_lock:  # as if acquire() were waiting on another process
        threading.Thread(target=lambda: (http_limits.make_session(), done.set()), daemon=True).start()
        assert done.wait(2)


def test_failed_store_setup_closes_its_connection(monkeypatch, tmp_path):
    closed = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    real_connect = sqlite3.connect
    monkeypatch.setattr(http_limits.sqlite3, "connect",
                        lambda *args, **kwargs: real_connect(*args, factory=TrackingConnection, **kwargs))
    monkeypatch.setattr(http_limits.os, "access", lambda path, mode: False)
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(tmp_path / "limits"))
    with pytest.raises(RuntimeError):
        http_limits.make_session()
    assert closed == [True]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_read_only_shared_store_fails_before_any_request(monkeypatch, tmp_path):
    directory = tmp_path / "limits"
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(directory))
    http_limits.make_session()
    monkeypatch.setattr(http_limits, "_connection", None)
    store = directory / "rate_limits.sqlite"
    store.chmod(0o444)
    directory.chmod(0o555)
    try:
        with pytest.raises(RuntimeError, match="PAPER_SEARCH_MCP_RATE_LIMIT_DIR"):
            CrossRefSearcher()
    finally:
        directory.chmod(0o755)
        store.chmod(0o644)


def test_unusable_shared_directory_fails_before_any_request(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("PAPER_SEARCH_MCP_RATE_LIMIT_DIR", str(blocker / "limits"))
    with pytest.raises(RuntimeError, match="PAPER_SEARCH_MCP_RATE_LIMIT_DIR"):
        CrossRefSearcher()
    assert "PAPER_SEARCH_MCP_RATE_LIMIT_DIR" in caplog.text


@pytest.fixture
def sent(monkeypatch):
    urls = []

    def send(self, request, *args, **kwargs):
        urls.append(request.url)
        return make_response(429)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return urls


@pytest.fixture
def acquired(monkeypatch):
    calls = []
    monkeypatch.setattr(http_limits, "acquire", lambda limit, max_wait, credentialed: calls.append(
        (limit.service, max_wait, credentialed)))
    return calls


def test_adapter_paces_listed_hosts_and_never_retries(sent, acquired):
    request = requests.Request("GET", "https://export.arxiv.org/api/query").prepare()
    assert http_limits.RateLimitedAdapter().send(request).status_code == 429
    assert len(sent) == 1
    assert acquired == [("arxiv", 10.0, False)]


@pytest.mark.parametrize(("url", "headers", "credentialed"), [
    ("https://api.openalex.org/works", {"Authorization": "Bearer key"}, True),
    ("https://api.openalex.org/works", {"Authorization": ""}, False),
    ("https://api.openalex.org/works", {}, False),
    ("https://polite.example/works?mailto=a@example.org", {}, True),
    ("https://polite.example/works?mailto=", {}, False),
])
def test_credential_is_read_from_the_request(sent, acquired, url, headers, credentialed):
    http_limits.register_service_limit(
        ("polite.example",), ServiceLimit("polite", 5, 1.0, credentialed_count=10, credential="mailto"))
    http_limits.RateLimitedAdapter().send(requests.Request("GET", url, headers=headers).prepare())
    assert acquired[0][2] is credentialed


def test_adapter_passes_unlisted_hosts_through(sent, acquired):
    http_limits.RateLimitedAdapter().send(requests.Request("GET", "https://example.org/x").prepare())
    assert len(sent) == 1
    assert acquired == []


def test_subclass_can_pace_unlisted_hosts_and_set_max_wait(sent, acquired):
    class ConsumerAdapter(http_limits.RateLimitedAdapter):
        max_wait = 2.5

        def limit_for(self, url):
            return super().limit_for(url) or ServiceLimit("default", 1, 3.0)

    ConsumerAdapter().send(requests.Request("GET", "https://example.org/x").prepare())
    assert acquired == [("default", 2.5, False)]


@pytest.mark.parametrize("connector", CONNECTORS, ids=lambda cls: cls.__name__)
def test_connector_session_uses_rate_limited_adapter(connector):
    assert isinstance(connector().session.get_adapter("https://example.org/"), http_limits.RateLimitedAdapter)


def test_make_session_uses_installed_factory_for_both_schemes():
    class ConsumerAdapter(http_limits.RateLimitedAdapter):
        pass

    http_limits.set_adapter_factory(ConsumerAdapter)
    session = http_limits.make_session()
    assert isinstance(session.get_adapter("https://api.crossref.org/works"), ConsumerAdapter)
    assert session.get_adapter("http://example.org/") is session.get_adapter("https://example.org/")
    http_limits.set_adapter_factory(None)
    assert type(http_limits.make_session().get_adapter("https://x.example/")) is http_limits.RateLimitedAdapter


def test_get_builds_a_fresh_session_through_the_installed_factory():
    # Like requests.get: no cookies or other state carry over between unrelated requests.
    adapters = []

    class ConsumerAdapter(http_limits.RateLimitedAdapter):
        def send(self, request, *args, **kwargs):
            return make_response(204)

    http_limits.set_adapter_factory(lambda: adapters.append(ConsumerAdapter()) or adapters[-1])
    assert http_limits.get("https://example.org/x", timeout=5).status_code == 204
    assert http_limits.get("https://example.org/x").status_code == 204
    assert len(adapters) == 2
