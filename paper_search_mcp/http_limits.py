"""Per-service rate limits for connector HTTP requests, optionally shared across processes."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter

from .config import get_env

logger = logging.getLogger(__name__)

# Well under server.SEARCH_PAPERS_SOURCE_TIMEOUT_SECONDS, so a busy service fails fast.
MAX_WAIT_SECONDS = 10.0


class RateLimited(requests.RequestException):
    """The next request slot is further away than the caller will wait."""


@dataclass(frozen=True)
class ServiceLimit:
    """At most count requests per window; credentialed_count when a request carries credential.

    credential names a request header or query parameter, such as an API key or contact address.
    """

    service: str
    count: int
    seconds: float
    credentialed_count: int | None = None
    credential: str | None = None

    def __post_init__(self) -> None:
        if self.count < 1 or self.seconds <= 0 or (
                self.credentialed_count is not None and self.credentialed_count < 1) or (
                (self.credentialed_count is None) != (self.credential is None)):
            raise ValueError(f"invalid rate limit for {self.service}")


# Keyed by lower-case host; hosts of one service share one entry.
SERVICE_LIMITS: dict[str, ServiceLimit] = {}


def register_service_limit(hosts: Iterable[str], limit: ServiceLimit) -> None:
    """Apply limit to each host, replacing any existing entry."""
    for host in hosts:
        SERVICE_LIMITS[host.lower()] = limit


# https://info.arxiv.org/help/api/tou.html: one request every three seconds.
register_service_limit(("export.arxiv.org", "arxiv.org", "www.arxiv.org"), ServiceLimit("arxiv", 1, 3.0))

# https://www.semanticscholar.org/product/api/tutorial: 1 request a second for a key. Anonymous
# requests share one pool among all anonymous users, with no per-client figure.
register_service_limit(("api.semanticscholar.org",), ServiceLimit("semantic_scholar", 1, 1.0))

# https://help.openalex.org/api/authentication/: 10 a second without a key, 30 with one.
register_service_limit(("api.openalex.org",), ServiceLimit(
    "openalex", 10, 1.0, credentialed_count=30, credential="Authorization"))

# https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/:
# 5 a second in the public pool.
register_service_limit(("api.crossref.org",), ServiceLimit("crossref", 5, 1.0))

# OpenAIRE's x-ratelimit-limit response header: 7,200 an hour without a key.
register_service_limit(("api.openaire.eu",), ServiceLimit("openaire", 7200, 3600.0))

# Zenodo's x-ratelimit-limit response header: 30 a minute.
register_service_limit(("zenodo.org",), ServiceLimit("zenodo", 30, 60.0))

# https://www.ncbi.nlm.nih.gov/books/NBK25497/: 3 a second without an API key.
register_service_limit(("eutils.ncbi.nlm.nih.gov",), ServiceLimit("pubmed", 3, 1.0))

# https://doaj.org/api/v4/docs: two requests a second on all API routes.
register_service_limit(("doaj.org",), ServiceLimit("doaj", 2, 1.0))


def _carries_credential(limit: ServiceLimit, request: requests.PreparedRequest) -> bool:
    if not limit.credential or limit.credentialed_count is None:
        return False
    if (request.headers.get(limit.credential) or "").strip():
        return True
    values = parse_qs(urlsplit(request.url or "").query).get(limit.credential, [])
    return any(value.strip() for value in values)


# Bound at import so that tests patching time.sleep or time.time elsewhere don't affect pacing.
# Slot times use the wall clock, which processes share; deadlines use the monotonic clock.
_clock = time.time
_monotonic = time.monotonic
_sleep = time.sleep
_connection: sqlite3.Connection | None = None
_connection_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        directory = get_env("RATE_LIMIT_DIR", "").strip()
        target = ":memory:"
        connection = None
        try:
            if directory:
                path = Path(directory).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                target = str(path / "rate_limits.sqlite")
            connection = sqlite3.connect(target, timeout=MAX_WAIT_SECONDS, isolation_level=None,
                                         check_same_thread=False)
            connection.execute("CREATE TABLE IF NOT EXISTS slots (service TEXT NOT NULL, at REAL NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS slots_by_service ON slots (service, at)")
            # An existing read-only store passes the statements above. SQLite also needs to write
            # its journal beside the file. Check permissions rather than take a lock that another
            # process may hold.
            if directory and not (os.access(path, os.W_OK) and os.access(target, os.W_OK)):
                raise PermissionError("store or its directory is not writable")
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise RuntimeError(
                f"PAPER_SEARCH_MCP_RATE_LIMIT_DIR={directory!r} can't hold shared rate limits "
                f"({exc}); fix or unset it"
            ) from exc
        _connection = connection
    return _connection


def acquire(limit: ServiceLimit, max_wait: float = MAX_WAIT_SECONDS,
            credentialed: bool = False) -> None:
    """Wait for and record a request slot; raise RateLimited rather than wait over max_wait."""
    if credentialed and limit.credentialed_count is not None:
        name, count = f"{limit.service}_credentialed", limit.credentialed_count
    else:
        name, count = limit.service, limit.count
    # One deadline across retries, so losing a slot to another waiter doesn't restart the wait.
    deadline = _monotonic() + max_wait
    while True:
        # Lock waits count against the deadline too; the floor covers ordinary millisecond
        # contention when a slot frees right at the deadline.
        remaining = max(deadline - _monotonic(), 0.1)
        if not _connection_lock.acquire(timeout=remaining):
            raise RateLimited(f"{limit.service}: rate-limit store busy")
        try:
            connection = _connect()
            try:
                connection.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
                # Take the write lock before counting, so processes sharing the file can't both
                # see room and both take the last slot.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    now = _clock()
                    # Slots from before a backward clock step count as taken now, capping the wait.
                    connection.execute("UPDATE slots SET at = ? WHERE service = ? AND at > ?",
                                       (now, name, now))
                    connection.execute("DELETE FROM slots WHERE service = ? AND at <= ?",
                                       (name, now - limit.seconds))
                    row = connection.execute(
                        "SELECT at FROM slots WHERE service = ? ORDER BY at DESC LIMIT 1 OFFSET ?",
                        (name, count - 1),
                    ).fetchone()
                    wait = 0.0 if row is None else row[0] + limit.seconds - now
                    if wait <= 0:
                        connection.execute("INSERT INTO slots VALUES (?, ?)", (name, now))
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            except sqlite3.DatabaseError as exc:
                # For example another process holding the shared file's lock past the timeout.
                raise RateLimited(f"{limit.service}: rate-limit store unavailable ({exc})") from exc
        finally:
            _connection_lock.release()
        if wait <= 0:
            return
        if _monotonic() + wait > deadline:
            raise RateLimited(f"{limit.service}: next request slot is {wait:.0f} s away")
        _sleep(wait)


class RateLimitedAdapter(HTTPAdapter):
    """Wait for a rate-limit slot before each request to a limited host; never retry."""

    max_wait = MAX_WAIT_SECONDS

    def limit_for(self, url: str) -> ServiceLimit | None:
        return SERVICE_LIMITS.get((urlsplit(url).hostname or "").lower())

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        limit = self.limit_for(request.url or "")
        if limit is not None:
            acquire(limit, self.max_wait, _carries_credential(limit, request))
        return super().send(request, *args, **kwargs)


_adapter_factory: Callable[[], HTTPAdapter] | None = None


def set_adapter_factory(factory: Callable[[], HTTPAdapter] | None) -> None:
    """Use factory() for sessions created later; None restores RateLimitedAdapter."""
    global _adapter_factory
    _adapter_factory = factory


def make_session() -> requests.Session:
    """A Session for connectors; fails before any request if the shared directory is unusable."""
    # Once connected, skip the lock: acquire() may hold it while waiting on another process.
    if _connection is None:
        with _connection_lock:
            try:
                _connect()
            except RuntimeError as exc:
                logger.error("%s", exc)
                raise
    adapter = (_adapter_factory or RateLimitedAdapter)()
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get(url: str, **kwargs: Any) -> requests.Response:
    """Rate-limited requests.get, likewise with a fresh session per call."""
    with make_session() as session:
        return session.get(url, **kwargs)
