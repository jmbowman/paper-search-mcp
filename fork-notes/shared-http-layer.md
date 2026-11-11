# Decision record: shared-http-layer

Proposal branch `shared-http-layer`, cut from upstream/main at cab1b5c. It routes every outbound
connector request through `paper_search_mcp/http_limits.py`, which paces each request to the
service's stated limit. This record is fork-only (on `integration`) and never goes on a proposal
branch. It records why the design is what it is, for later sessions and for answering the upstream
maintainer.

## Context

- **Consumer.** The prior-art-search skill in jmbowman/agentic_development uses the connectors as a
  library behind its own shim: host blocks, a query log, redaction, and its own retries. See that
  repo's `docs/tool-feedback/paper-search-mcp.md` and
  `docs/prior-art/2026-11-07-search-harvest-pipeline.md`.
- **Problem.** Before this branch, only a few connectors paced their requests, each in its own way,
  and the pacing held only within one process. Every MCP client starts its own server, and the
  CLI's `--source-timeout` runs each source in a worker process, so one machine could send a
  service several times its limit.
- **Upstream maintainer (universea).** The project has a single maintainer, who often rebuilds
  outside PRs into their own commits.
  - They lean against new dependencies (#144).
  - Their recent PRs prefer failing fast and honestly over sleeping in workers, and errors over
    empty results: #125, #130, #144 (Google Scholar doesn't sleep in a worker past 30 s), #146
    ("rate-limited providers were not retried") and #147 (downloads are never retried).
  - Their house style is one-line module docstrings, terse docstrings on key public functions,
    settings read through `config.get_env`, named timing constants, and prose commit messages.

## Decisions, with the alternatives rejected

1. **The adapter paces but never retries.**
   - *Rejected:* retrying 429 and 503 in the adapter after Retry-After, which was the first
     prototype.
   - *Why:* the review found that it broke `openalex_relations`' documented "no automatic retries"
     promise (`openalex_relations.py:118`), slept past the server's 45 s per-source timeout while
     holding arXiv's lock, and made OpenAIRE resend to a throttled service. It also ran against
     upstream's fail-fast direction.
   - Retries stay in each connector. The consumer's shim adds its own retries by subclassing the
     adapter.

2. **One limiter written on stdlib SQLite, not PyrateLimiter.**
   - *Rejected:* PyrateLimiter as a core dependency, and also as an optional extra with a built-in
     fallback, which meant two code paths.
   - *Why:* the only thing needed from it was atomic reservation across processes. Its SQLite
     bucket counts and then inserts under a Python lock, which is why it needs `filelock`
     (sqlite_bucket.py:122-154 at 4.5.1). It leaves threads combined with the file lock as "TBD"
     (:242-245). It is 3,662 lines, and had a breaking 4.0 release in January 2026.
   - `BEGIN IMMEDIATE` takes SQLite's write lock before the count, so the reservation is atomic
     with no extra dependency. Probe: 4 processes × 2 threads at 10 a second gave 40 acquisitions
     in 3.91 s.
   - Precedent: upstream's own `search_cache.py` already coordinates threads and processes through
     SQLite transactions.

3. **In memory by default; a shared file only when PAPER_SEARCH_MCP_RATE_LIMIT_DIR is set.**
   - It's one implementation either way; only the connection target differs.
   - In memory stays the default because a library shouldn't write to disk unasked, read-only
     containers and network home directories (where SQLite locking is unreliable) need to work,
     and tests need isolation. Making the shared file the default could be pitched upstream later.
   - The setting is read through `get_env` to match house style, so the unprefixed
     `RATE_LIMIT_DIR` also works. Its generic name could collide with another tool; Jeremy
     accepted that.

4. **An unusable shared directory fails loudly at session creation.**
   - *Rejected:* falling back to in-memory limits with a warning.
   - *Why:* Jeremy's requirement is that cross-process protection must never be lost silently.
   - `_connect()` checks write permission on the store and its directory (`os.access`), because a
     read-only existing store passes `CREATE TABLE IF NOT EXISTS`. A write-transaction probe was
     tried first. `/code-review` 3 rejected it: another process holding the lock could make a
     valid directory look misconfigured and stop the server starting.
   - `make_session()` takes the connection lock only before the first connection, so it never
     queues behind an `acquire()` that is waiting on another process. `make_session()` logs the error
     as well as raising it, because some connectors construct other searchers inside broad
     `except` blocks (SSRN builds `OpenAlexSearcher` this way) that would swallow it.

5. **Fail fast after MAX_WAIT_SECONDS (10 s) in total; never sleep longer.**
   - It's one deadline for each `acquire()` call. A per-attempt check let losing waiters restart
     the wait, so with N waiters a thread could be held for about N windows.
   - The deadline uses `time.monotonic`. Slot timestamps use the wall clock, because only wall
     time is shared across processes. A wall-clock deadline let a backward NTP step during a wait
     remove the cap (`/code-review` 4).
   - Waits are clamped against backward clock steps: slots "in the future" are pulled back to now.
     Before that fix, a one-hour step back slept for 3,603 s.
   - The deadline also covers lock waits, so each pass waits only for the call's remaining time:
     - for the process-wide connection lock;
     - for SQLite's busy timeout (`PRAGMA busy_timeout`), which covers another process's lock.

     There is a 100 ms floor for ordinary contention when a slot frees right at the deadline.
     Before this, the k-th thread queued behind a locked shared store waited about k × 10 s
     (thermos 3). Any `sqlite3.DatabaseError` becomes `RateLimited`, a `RequestException`.
   - It's a constant, not a setting, because upstream makes no timing value configurable, and it
     must stay below the server's non-configurable 45 s source timeout.
   - The consumer can override it per adapter, through a subclass's `max_wait`.

6. **One window per service, with a credentialed count read from the request itself.**
   - *Rejected:* several windows per service, since no service needs them, and choosing the
     credentialed tier from an environment variable.
   - *Why the request:* a key passed to `OpenAlexSearcher(api_key=...)` must count, and an empty
     key must not. `credential` names a header or query parameter: OpenAlex sends
     `Authorization: Bearer`. Branch 2 will use `mailto` for Crossref's polite pool.
   - Keeping separate keyed and anonymous counts is deliberate: part of the point is to use the
     higher limits keys earn, without requiring everyone to get keys.

7. **One central table, keyed by host, not registered from each connector.**
   - *Why:* hosts are shared. PubMed and PMC both use E-utilities. `api.openalex.org` serves the
     OpenAlex connector, `openalex_relations` and SSRN search. arXiv PDF links reach the server
     through other connectors and the fallback download.
   - Registering from connectors would make a limit depend on which modules happen to be imported.
   - The citation for each limit is a comment, not a field.
   - Limits that came from response headers rather than documentation: OpenAIRE (7,200 an hour)
     and Zenodo (30 a minute), both read 2026-11-05.
   - No entry for CORE (its docs return 403), IEEE (limits are shown only at registration) or SSRN
     (no stated limit, and its old throttle was never called).

8. **Unlisted hosts pass through unchanged.** This preserves WoS and Scopus's deliberate "never retry
   against quota limits" policy. A consumer paces other hosts by overriding `limit_for()`.

9. **The consumer API is kept small:** `register_service_limit`, `set_adapter_factory` (a
   no-argument factory), `RateLimitedAdapter.limit_for` and `max_wait`, `acquire`, `RateLimited`,
   `make_session` and `get`.
   - Removed after the quality review: `set_default_limit`, which an overridable `limit_for`
     replaces, and `adapter_kwargs`, which had no caller. The consumer agreed.
   - `get()` builds a fresh session for each call through the current factory, as `requests.get`
     does, so cookies don't carry over between unrelated requests.

10. **Everything goes through the layer, including Sci-Hub and the server's fallback download.**
    - Sci-Hub is a deliberate exception to the fork's no-touching rule. It's a one-line change,
      and it lets the consumer's host block reach Sci-Hub.
    - The fallback download moved from httpx to `requests` in a worker thread. The maintainer
      added httpx there in 2a81648, the same commit that moved every other call to
      `asyncio.to_thread` over `requests`. Side effects:
      - the User-Agent changes;
      - a cancelled download finishes in its thread instead of stopping at once;
      - `requests` applies a matching `~/.netrc` entry's credentials, including on redirect hops,
        which httpx didn't. Other `requests`-based connectors already behaved this way, and upstream
        blocks it only in OpenReview and SSRN (`_AnonymousAuth`, `_PublicOnlyAuth`).
        - A full fix is awkward: an explicit `auth=` doesn't stop `rebuild_auth` from re-applying
          netrc on redirect, and `trust_env=False` would also drop proxy settings.
        - Offer the maintainer a follow-up if they care.
    - For arXiv hosts, the fallback download takes `ArxivSearcher._request_lock`, with a
      MAX_WAIT_SECONDS timeout, so arXiv's one-connection rule holds within one process. A
      redirect *onto* arxiv.org is paced but not locked, which is a known limit.

11. **arXiv:** the interval timestamp is removed and the class lock kept, because its terms of use
    ask for one connection at a time. arXiv re-raises `RateLimited` instead of retrying and
    returning `[]`. PDF downloads share the API's bucket and are never retried (#147, which pins
    one request even for 429 and 503).

12. **No fork handler.** Nothing in the package forks; the CLI and section workers are exec'd.
    Closing an inherited SQLite handle in a child process can damage the parent's journal. Library
    users must not reuse sessions across `fork()`.

## Moved out of scope (separate PRs)

- `retry-after-parser`: Retry-After in both forms for Semantic Scholar and IEEE. Saved as
  `.worktrees/retry-after-followup.patch`. Semantic Scholar's date-form waits need a cap.
- `arxiv-pdf-canonical-url`: request `https://arxiv.org/pdf/<id>` instead of `.../<id>.pdf`.
  The `.pdf` form answers 301 to the bare form, so each download takes two of arXiv's 1-per-3-s
  slots and costs an extra 3 s. Pacing each hop is correct under arXiv's rules; this just avoids the
  redundant request. It changes a URL that #147's download tests assert.
- `ssrn-dead-helpers`: remove the uncalled `_throttle()` and `_fetch_page()`. The maintainer kept
  them deliberately in 870d8a0, so the PR has to argue for removing them.

## Known limits, to mention in the PR

- Several connectors swallow `RequestException` into `[]`, so `RateLimited` can look like
  "no results" there. That's upstream's existing behavior for network errors. arXiv is fixed.
- The Semantic Scholar and DOAJ download tools call connectors synchronously inside async server
  tools (an upstream pattern), so limiter waits, at most 10 s, can pause the event loop.
- One connection at a time for arXiv holds only within a process, not across processes.
  - Within a process, only `ArxivSearcher` and the server's fallback download take the lock.
  - Other code paths that reach arxiv.org are paced but not locked. Examples: Semantic Scholar's
    PDF download, which rewrites `arxiv.org/abs/` links to PDF links; any connector's arXiv
    `pdf_url`; redirects onto arxiv.org.
  - Covering these needs a per-service concurrency limit in the shared layer. That was rejected:
    it's a design expansion, and the adapter can't tell when a streamed body has been consumed.

## Review history

- Prototype by the agentic_development session: PyrateLimiter plus retries in the adapter.
- `/code-review` 1: the retry problems led to the pace-only redesign, and the limiter swap.
- thermos 1: one window, citations as comments, the hooks reduced, fail-fast added, loud failure
  for an unusable directory, Retry-After split out.
- `/code-review` 2: arXiv RateLimited, the fallback lock, store errors, credential from the request,
  SSRN hunk reverted.
- thermos 2: one deadline per call, the writability check, the fork handler dropped,
  DatabaseError, www.arxiv.org, the lock wait capped.
- `/code-review` 3: the README no longer promises a visible rate-limit error from every connector;
  the writability check uses `os.access` instead of a lock probe; `make_session()` skips the lock
  once connected. The redirect double slot went to a follow-up. This reviewer sent one live HEAD
  request to arxiv.org despite the no-network rule, so future review prompts must forbid network
  access explicitly.
- thermos 3:
  - Fixed: lock waits now count against `max_wait`.
  - Documented: the `.netrc` side effect of the httpx switch.
  - Polish: `X | None` typing, one parametrized credential test, the arXiv download-URL assertion
    dropped.
  - The quality reviewer judged the branch ready for upstream, with nothing structural left.
- `/code-review` 4 (final):
  - Fixed: the monotonic deadline; `ServiceLimit` now rejects a `credentialed_count` without a
    `credential`, or the reverse; a failed `_connect()` closes its connection.
  - Documented: direct arxiv.org paths that skip the lock.
  - Rejected:
    - WAL mode with `synchronous=OFF` for the shared file: it saves 10-50 ms per request against
      HTTP requests that take hundreds of milliseconds, and risks a corrupt store that would
      rate-limit everything.
    - Repeats of settled items.
  - The review rounds stopped here: the reviewers were re-raising rejected items.
- Rejected repeatedly, as settled:
  - unittest-style tests in upstream's unittest files. Upstream's style wins over the global
    pytest rule in these files; new pytest-style test files are fine.
  - Reverting the Sci-Hub change.
  - Reading the shared-directory setting only with its prefix.
  - Dropping query-parameter credentials.
  - Dropping the log line for an unusable directory.
  - A session per `get()` call as an "inefficiency" (it matches `requests.get`).
  - Moving arXiv's one-connection lock into the layer, and the duplicated host lookup in
    `server.py`.
