# deputy-mcp

**An MCP server for [Deputy](https://www.deputy.com) — ask about rosters, timesheets and shifts, and manage your own, from Claude or any MCP client.**

[![CI](https://github.com/augbastos/deputy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/augbastos/deputy-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> PyPI: coming soon. Until the first release is published, install from source (see [Quickstart](#quickstart)).

deputy-mcp exposes Deputy's workforce data to a language model through the [Model Context Protocol](https://modelcontextprotocol.io): nine read tools for schedules, timesheets, people and locations, plus five write tools (clock in/out, claim an open shift, request a swap, set unavailability) that stay hidden until you explicitly opt in. It runs locally, talks only to your own Deputy install, and inherits exactly the permissions of the token you give it.

---

## Why this exists

Deputy runs the rosters of a lot of shift-based workplaces, but there was no open-source MCP server for it. As of July 2026 the official MCP registry returns no results for Deputy, and the only Deputy MCP connectors on offer are proprietary hosted services — you send your workforce data through someone else's servers to use them. deputy-mcp is the local, auditable alternative: MIT-licensed, runs on your machine, and its only outbound traffic is to your own Deputy instance.

---

## Quickstart

You need two things before connecting: a **Deputy API token** and your **base URL**.

- **Token** — in Deputy, go to **Business settings → Integrations → API access**, create a **New OAuth Client**, then **Get an Access Token** (Deputy shows it once — copy it). This is a permanent token; it inherits your own Deputy permissions.
- **Base URL** — the address you see in the browser when you are logged in to Deputy, e.g. `https://your-company.eu.deputy.com`. The pattern is `https://{install}.{geo}.deputy.com` (`geo` is your region, such as `au`, `eu`, `uk`, `na`). A trailing slash or an `/api/v1` suffix is accepted and normalized away.

### Claude Code

```bash
claude mcp add deputy \
  -e DEPUTY_API_TOKEN=your-deputy-token \
  -e DEPUTY_BASE_URL=https://your-company.eu.deputy.com \
  -- uvx deputy-mcp
```

Add `-e DEPUTY_ALLOW_WRITES=true` if you want the write tools (see [Security & privacy](#security--privacy)).

### Claude Desktop

Add the server to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "deputy": {
      "command": "uvx",
      "args": ["deputy-mcp"],
      "env": {
        "DEPUTY_API_TOKEN": "your-deputy-token",
        "DEPUTY_BASE_URL": "https://your-company.eu.deputy.com"
      }
    }
  }
}
```

### Any other MCP client

deputy-mcp speaks MCP over **stdio**. Point your client at the command `uvx deputy-mcp` (or `deputy-mcp` once installed) and pass the `DEPUTY_*` environment variables listed under [Configuration](#configuration).

### From source (while PyPI publish is pending)

Until the package is on PyPI, install and run it straight from GitHub with `uvx`:

```bash
uvx --from git+https://github.com/augbastos/deputy-mcp deputy-mcp
```

Or wire that same command into Claude Code:

```bash
claude mcp add deputy \
  -e DEPUTY_API_TOKEN=your-deputy-token \
  -e DEPUTY_BASE_URL=https://your-company.eu.deputy.com \
  -- uvx --from git+https://github.com/augbastos/deputy-mcp deputy-mcp
```

---

## See it work

The excerpts below are a captured run of the companion CLI against the bundled mock harness (`examples/mock_deputy.py`), which serves **fictional** data on loopback — no live Deputy instance. Names and companies (Cloud Nine Cafe, Alex Rivera, Sam O'Brien) are made up. Times are UTC.

```console
$ deputy-mcp whoami
Authenticated as: Alex Rivera
  UserId: 201
  EmployeeId: 101
  Company: 1
  CompanyName: Cloud Nine Cafe
Primary location: Cloud Nine Cafe

$ deputy-mcp roster
My roster 2026-07-17 to 2026-07-24:
  - 2026-07-17 20:27-00:27 UTC  Alex Rivera  (area #11)
  - 2026-07-18 09:00-17:00 UTC  Alex Rivera  (area #11)

$ deputy-mcp who
As of 2026-07-17T21:28:40+00:00:

Clocked in (1):
  - 2026-07-17 20:27---:-- UTC  Alex Rivera  0.00h (in progress)

Rostered now (2):
  - 2026-07-17 20:27-00:27 UTC  Alex Rivera  (area #11)
  - 2026-07-17 19:27-23:27 UTC  Sam O'Brien  (area #12)

$ deputy-mcp next
Next shift: 2026-07-18 09:00-17:00 UTC  Alex Rivera
```

---

## Tool reference

Every tool accepts a `response_format` argument — `"markdown"` (human-readable, the default) or `"json"` (raw records). Optional arguments are marked `?`. Read tools never mutate Deputy.

### Read tools (always available)

| Tool | Arguments | Returns |
|------|-----------|---------|
| `deputy_whoami` | — | Who the token authenticates as, plus company/location and its timezone. Run this first to confirm setup. |
| `deputy_get_my_roster` | `start_date?`, `end_date?` | Your own scheduled shifts in a date range (defaults to today through +7 days). |
| `deputy_get_team_roster` | `date?`, `start_date?`, `end_date?`, `area_id?` | Every scheduled shift in a range (or a single `date`), optionally scoped to one area. |
| `deputy_who_is_working` | `at?` | Snapshot at an instant (default now): who is clocked in vs who is rostered on. |
| `deputy_get_employee_info` | `name_or_id` | Profile(s) for employees matching a name substring or numeric id, each listed with its id. |
| `deputy_search_shifts` | `employee?`, `area_id?`, `start_date?`, `end_date?`, `open_only?`, `limit?`, `offset?` | Shifts filtered by person, area, date range and open status; paginated (max 500 per page). |
| `deputy_get_areas` | — | All areas (operational units / work locations) with their ids. |
| `deputy_next_shift` | `employee?` | The single next upcoming shift for you (default) or a named/numbered employee. |
| `deputy_get_my_timesheets` | `start_date?`, `end_date?` | Your own timesheets — actual worked time — with a worked-hours total (defaults to the last 7 days). |

### Write tools (opt-in)

Write tools are **only registered when `DEPUTY_ALLOW_WRITES=true`**. While writes are disabled they are invisible to the client — a language model cannot even see that they exist. Every write acts as the signed-in token holder; none of them delete anything.

| Tool | Arguments | Returns |
|------|-----------|---------|
| `deputy_claim_open_shift` | `shift_id` | Assigns you to an open (unassigned) shift by filling its roster. |
| `deputy_request_shift_swap` | `shift_id`, `note?` | Offers one of your shifts up for swap; creates a request pending manager approval. |
| `deputy_set_unavailability` | `start`, `end`, `reason?`, `repeat?` | Records an unavailability window (one-off, or recurring via an iCal `RRULE`). |
| `deputy_clock_in` | `area_id?` | Starts a live timesheet against an area (`area_id` auto-resolved only when the install has a single rosterable area). |
| `deputy_clock_out` | `mealbreak_minutes?` | Ends your single in-progress timesheet, optionally recording a meal break. |

---

## Who is this for

**Shift workers** — check your own schedule in plain language, no app-hunting:
> "When do I work next?" · "Am I on with Alex this week?" · "How many hours did I work last week?"

**Team leads** — real-time coverage and gaps at a glance:
> "Who's on right now?" · "Are there any open shifts on Saturday?" · "Show me the team roster for tomorrow in the kitchen area."

**Small business owners** — quick answers without opening the Deputy UI:
> "Is anyone clocked in over at the second location?" · "Who's scheduled this weekend?"

**Developers** — the async Deputy client underneath the MCP layer is a reusable, MCP-free library. Point it at your install and call it directly:

```python
import asyncio
from deputy_mcp.client import DeputyClient

async def main() -> None:
    async with DeputyClient.from_env() as deputy:  # reads DEPUTY_* env vars
        print(await deputy.next_shift())

asyncio.run(main())
```

---

## Security & privacy

- **Writes are opt-in and off by default.** A workforce system a model can drive should not be able to clock you in or give your shift away unless you asked for that. Set `DEPUTY_ALLOW_WRITES=true` to enable the five write tools; leave it unset and they are never registered.
- **The token is your permissions.** deputy-mcp does exactly what your Deputy account can do — no more. Use a token from **your own account** for personal use; do not hand it an admin or service-account token "just in case", because the model then inherits that reach.
- **Fail-closed host policy.** `DEPUTY_BASE_URL` must resolve to a `*.deputy.com` host or startup refuses, so a typo can't quietly point your token at some other server. Legitimate enterprise custom domains opt back in with `DEPUTY_ALLOW_CUSTOM_HOST=true`.
- **Runs locally, zero telemetry.** The server runs on your machine and phones nothing home. Its only network traffic is HTTPS to your own Deputy install. Your colleagues' roster data stays between you and Deputy — it never passes through any third party. The token is held as a redacted secret and is never logged or printed.

---

## Configuration

All configuration comes from `DEPUTY_*` environment variables. Copy [`.env.example`](.env.example) to `.env` and fill it in (never commit `.env` — it holds the token).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DEPUTY_API_TOKEN` | Yes | — | Deputy permanent token or OAuth access token (stored redacted). |
| `DEPUTY_BASE_URL` | Yes | — | Your install origin, e.g. `https://your-company.eu.deputy.com`. A trailing slash or `/api/v1` suffix is normalized away. |
| `DEPUTY_ALLOW_WRITES` | No | `false` | Enable the write tools. Accepts `true`/`1`/`yes` (case-insensitive). |
| `DEPUTY_ALLOW_CUSTOM_HOST` | No | `false` | Allow a `base_url` host outside `*.deputy.com` (enterprise custom domains only). |
| `DEPUTY_CACHE_TTL` | No | `30` | In-memory read-cache lifetime in seconds; `0` disables caching. |
| `DEPUTY_TIMEOUT` | No | `30` | Per-request HTTP timeout in seconds. |
| `DEPUTY_MAX_RETRIES` | No | `3` | Max automatic retries on `429`/`5xx`/transport errors (with backoff). |

---

## CLI (bonus)

The same client is available as a small standalone CLI — handy for a quick check or a shell script, no MCP client required. It reads the same `DEPUTY_*` environment variables and adds `--json` for raw output.

```bash
deputy-mcp whoami                          # authenticated user + location
deputy-mcp roster --start 2026-07-20       # your roster (add --team for everyone, --area ID to scope)
deputy-mcp timesheets --end 2026-07-17     # your timesheets
deputy-mcp who                             # who is working right now
deputy-mcp areas                           # list areas / operational units
deputy-mcp next --employee "Alex Rivera"   # next upcoming shift (defaults to you)
```

With no subcommand, `deputy-mcp` launches the MCP server on stdio (equivalent to `deputy-mcp serve`).

---

## Development

Requires [uv](https://docs.astral.sh/uv/). Clone the repo, then:

```bash
uv sync                        # install into a local .venv
uv run pytest                  # run the test suite (212 tests)
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy                    # type-check (strict)
```

CI runs the same gates on Linux (Python 3.11/3.12/3.13) and Windows (3.13). The client layer is MCP-free by design, so it stays reusable outside the server.

Run the server in a container:

```bash
docker compose run --rm deputy-mcp   # after copying .env.example to .env
```

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Roadmap

Planned work and known gaps live in [ROADMAP.md](ROADMAP.md).

## License

[MIT](LICENSE) © Augusto Bastos.
