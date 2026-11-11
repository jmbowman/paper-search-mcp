# paper-search-mcp (fork)

A fork of [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp), kept for one
consumer: the prior-art-search skill in jmbowman/agentic_development, which uses the connectors as a
library behind its own rate-limiting shim. Feedback on upstream lives there, in
`docs/tool-feedback/paper-search-mcp.md`.

## Branches

- `main` mirrors `upstream/main`. Never commit to it.
- One branch per upstream proposal, cut from `upstream/main`, each small enough to be one pull
  request. They never contain this file.
- `integration` (the default branch) holds this file and merges the proposal branches. Consumers pin
  its commits. When upstream merges a proposal, drop that branch from the integration.

## Working rules

- Changes aimed at upstream follow upstream's CONTRIBUTING.md: deterministic tests for behavior
  changes, live-network checks reported separately, credit for any code taken from other forks or PRs.
- Tests never contact real services. Run only offline tests, with non-local connections refused (a
  socket-blocking pytest plugin passed with `-p`); never run the MCP-server or end-to-end tests, which
  start subprocesses the guard doesn't reach.
- Treat the Sci-Hub and any other shadow-library connectors as out of scope: don't extend or call
  them, or run live tests against them. Mocked tests that CI runs are fine, and so are mechanical
  changes applied to every connector.
- Keep each proposal's diff minimal and in upstream's style; consumer-specific behavior (query logs,
  host blocks) belongs in the consumer's shim, reached through the session factory's adapter hook.
- Agents don't commit, push or change branches; the developer does. Draft `commit-message.md`
  (add it to `.git/info/exclude`).
- Before changing a proposal branch, read its decision record in `fork-notes/<branch>.md` (on
  `integration` only, never on the proposal branch), and update it after each review round.
