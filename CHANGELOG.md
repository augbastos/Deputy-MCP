# Changelog

All notable changes to Deputy MCP. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/) (pre-1.0: a minor version may break).

## [0.2.0] — unreleased

### Changed — breaking

- **FastMCP 4 and MCP Python SDK 2.** The server speaks MCP `2026-07-28` with current
  clients and the handshake revisions up to `2025-11-25` with older ones. Requires
  `pydantic>=2.12`.
- **OAuth tokens move to the OS keychain** (Windows Credential Manager, macOS Keychain,
  Secret Service). An existing `~/.deputy-mcp/token.json` is migrated on first use and
  deleted. Set `DEPUTY_TOKEN_STORE` to keep using a plaintext file.
- **`deputy_whoami` JSON is a curated summary** (`name`, `employee_id`, `company_name`,
  `timezone`, `clocked_in`, `calendar_feed_available`) instead of the raw `/me` record,
  and no longer includes the calendar-feed link.
- **Employee JSON is projected** to id, names, active flag, location and role, including
  the employee joined onto rosters and timesheets.

### Added

- Human confirmation, through MCP elicitation, before `deputy_claim_open_shift` assigns a
  shift.
- Central redaction of error text shown to models and printed by the CLI.
- Deterministic agent-interface evals (`pytest -m evals`).
- CI: Windows on Python 3.11 and 3.13, coverage floor, `pip-audit`, CodeQL, gitleaks,
  package and container checks, SBOM, Dependabot.
- `SECURITY.md`, this changelog, human-readable tool titles.

### Fixed

- **OAuth refresh** now calls the user's install (`/oauth/access_token`) with the
  redirect URI, as Deputy documents. It previously posted to `once.deputy.com`, which
  answers a refresh with "We did not detect 'code' in POST call", so an expired access
  token meant logging in again.
- A token loaded from the keychain or token file is checked against the `*.deputy.com`
  allowlist before use; previously a tampered store could redirect the bearer token.
- A login performed while the server runs takes effect without a restart, and a token
  already rotated by another process is adopted instead of refreshed twice. The keychain
  is read in a worker thread, not on the event loop.
- Redaction examines only the head of an error body, so a hostile multi-megabyte body
  can no longer stall the server.
- `deputy-mcp next --employee NAME` no longer picks the first of several matching
  employees.
- Your own shifts read "You" in API mode, as they already did in iCal mode.
- Self-service roster reads no longer make an admin-only area lookup first.
- The server no longer prints FastMCP's banner, which also made an update check to PyPI
  on every start.
- The source distribution contains only the project, not files from the working tree.

## [0.1.0]

First public version: 11 read tools and 5 opt-in write tools over Deputy's API; API
token, OAuth and iCal-feed authentication; standalone async client and CLI.
