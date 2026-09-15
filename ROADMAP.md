# Roadmap

What Deputy MCP may do next. Nothing here ships today; for current behaviour see the
[README](README.md), and for what changed between versions see the
[CHANGELOG](CHANGELOG.md). An item moves out of this file when it ships.

## Validation gaps to close first

The self-service reads, OAuth login and employee-level permission handling have been
validated against a live Deputy install with an employee account. These have not:

- **Manager-level reads.** Team roster, who is working, employee lookup and shift search
  have only been seen returning their permission error. The QUERY association name
  used to attach employee names (`EmployeeObject`) is Deputy's documented example and
  is probed by the live suite, but still needs a manager or admin token to confirm.
- **Write tools.** Clock in/out, unavailability, swap requests and open-shift claims are
  built on Deputy's documented `/supervise` and Resource endpoints and are covered by
  mocked tests only. They should be exercised against a trial install before anyone
  relies on them.
- **OAuth refresh.** 0.2.0 moved refreshes to the install-scoped token endpoint Deputy
  documents. The next live run should confirm a full 24-hour refresh and rotation cycle.
- **Rate limits and error bodies.** Deputy documents neither. Retry and backoff are
  conservative defaults, and error bodies are treated as untrusted text, until a live
  install shows real throttling behaviour.

## Manager workflows with confirmation

Approving timesheets, and approving or declining swap requests, would turn the server
into something a shift manager can run their day from. These act on other people's
pay and schedules, so they would reuse the elicitation gate that open-shift claims
already use, sit behind a second opt-in on top of `DEPUTY_ALLOW_WRITES`, and show
exactly which timesheets or swaps a confirmation covers.

## Change notifications

Every read today is pull-based. Deputy can send webhooks on roster and timesheet
changes; a small, separate receiver could turn "your Saturday shift moved" into an MCP
notification or a recent-changes resource. It needs an inbound public URL and webhook
signature verification, so it would not live in the stdio server.

## Several installs in one server

One process serves one Deputy install. People working across franchises or client
businesses would benefit from named installs selected per call. OAuth already returns
each install's endpoint, so the work is mostly a keyed client pool and a per-install
entry in the token store.

## Streamable HTTP transport

stdio suits a desktop client. A remote deployment would use MCP's Streamable HTTP
transport with MCP authorization in front of it, so that the Deputy OAuth token never
reaches the MCP client. FastMCP 4 provides both; the open questions are hosting and
per-user token isolation.

## Distribution

- Publish to PyPI, so `uvx deputy-mcp` works without a Git URL.
- List the server in the MCP Registry using the `server.json` already in the
  repository.
- An MCPB desktop bundle with a form for the credentials, for people who do not use a
  terminal.

No registry listing or package page will be linked from the README until it exists.
