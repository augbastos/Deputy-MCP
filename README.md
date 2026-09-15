# Deputy MCP

<!-- mcp-name: io.github.augbastos/deputy-mcp -->

[![CI](https://github.com/augbastos/Deputy-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/augbastos/Deputy-MCP/actions/workflows/ci.yml)
[![Security](https://github.com/augbastos/Deputy-MCP/actions/workflows/security.yml/badge.svg)](https://github.com/augbastos/Deputy-MCP/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Ask Claude *"when do I work next?"* and get the answer from your
[Deputy](https://www.deputy.com) roster.

Deputy MCP is a local [MCP](https://modelcontextprotocol.io) server that lets AI agents
read your shifts, timesheets and team schedule, and, only if you turn it on, act on them.
It runs on your machine, talks only to your Deputy install, and works with a regular
employee account.

## What you can ask

- "When do I work next?"
- "How many hours did I work last week?"
- "Who's on shift right now?" *(manager access)*
- "Clock me in." *(writes enabled)*
- "Take the open shift on Saturday." *(writes enabled; asks you to confirm first)*

## Install

Needs [uv](https://docs.astral.sh/uv/). For Claude Code:

```bash
claude mcp add deputy \
  -e DEPUTY_API_TOKEN=your-deputy-token \
  -e DEPUTY_BASE_URL=https://your-company.eu.deputy.com \
  -- uvx --from git+https://github.com/augbastos/Deputy-MCP deputy-mcp
```

Claude Desktop, other MCP clients and Docker: see [docs/DESIGN.md](docs/DESIGN.md#other-ways-to-install).

## Sign in

Pick one:

| Mode | Who it's for | What to set |
|---|---|---|
| **API token** | Deputy admins | `DEPUTY_API_TOKEN` and `DEPUTY_BASE_URL` |
| **OAuth** | Any employee | `DEPUTY_OAUTH_CLIENT_ID` and `DEPUTY_OAUTH_CLIENT_SECRET`, then run `deputy-mcp login` |
| **Calendar feed** | No API access | `DEPUTY_CALENDAR_URL` (your own roster only, read-only) |

For OAuth, create a personal app at <https://once.deputy.com/my/oauth_clients> with the
redirect URI `http://localhost:8823/callback`. Tokens are stored in your OS keychain.
All settings are listed in [`.env.example`](.env.example).

## Tools

- **Your data** (any account): `deputy_whoami`, `deputy_get_my_roster`,
  `deputy_next_shift`, `deputy_get_my_timesheets`, `deputy_get_my_colleagues`,
  `deputy_get_my_calendar_url`, `deputy_get_areas`
- **Team** (manager access): `deputy_get_team_roster`, `deputy_who_is_working`,
  `deputy_get_employee_info`, `deputy_search_shifts`
- **Actions** (only with `DEPUTY_ALLOW_WRITES=true`): `deputy_clock_in`,
  `deputy_clock_out`, `deputy_set_unavailability`, `deputy_request_shift_swap`,
  `deputy_claim_open_shift`

## Safe by default

- **Read-only** unless you set `DEPUTY_ALLOW_WRITES=true`; until then the action tools
  don't exist for the agent.
- **You confirm** before a shift is claimed, since that skips Deputy's approval step.
- **No guessing**: if a name matches several people, you get the list, not the first one.
- **Your credentials stay local**: tokens go only to `https://*.deputy.com` and live in
  your OS keychain; errors are scrubbed of secrets before an agent sees them.

## Status

The everyday read tools and OAuth sign-in were tested against a real Deputy install with
an employee account. Manager tools, actions and the OAuth token refresh are covered by
automated tests but not yet verified on a live install. Not affiliated with Deputy.

## More

- [How it's built and why](docs/DESIGN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) · [Roadmap](ROADMAP.md)

## License

[MIT](LICENSE)
