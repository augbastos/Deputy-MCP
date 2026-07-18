# Roadmap

This is where deputy-mcp is headed, not what it does today. Everything below is
**planned** — none of it ships in the current release, and nothing here should be read
as a feature you can use now. For what actually works today, see the
[README](README.md).

Status legend: every item is **Planned** unless it says otherwise. There are no
"beta" or "in progress" items yet; when one starts, its status changes here first.

A note on honesty, because it shapes several items below: the server has **not** been
smoke-tested against a live Deputy install yet. It is built against Deputy's published
Resource API docs and exercised by an entirely respx-mocked test suite, but a handful of API
behaviours are documented ambiguously or not at all (see
[Smoke-test-driven hardening](#smoke-test-driven-hardening)). Where a roadmap item
depends on pinning down real API behaviour, that dependency is stated.

---

## Deputy webhooks → push notifications on roster changes

**Status: Planned.**

Today every read is pull-based: a client asks, the server queries Deputy, the answer
comes back. That is fine for "what's my roster?" but useless for "tell me when my
shift changes." Deputy can emit webhooks on record changes, so the plan is a small
opt-in HTTP endpoint that receives roster/timesheet change events and surfaces them as
MCP notifications (or a queryable "recent changes" tool) — so an assistant can proactively
flag "your Saturday shift moved" instead of only answering when asked.

*Why it matters:* the difference between a lookup tool and something that actually
watches your schedule for you. Roster churn (swaps, cancellations, published changes)
is exactly the thing a worker wants pushed, not polled.

*Rough shape:* a receiver process (separate from the stdio MCP server, since it needs
an inbound public URL), webhook signature verification, a subscription-registration
helper, and a mapping from Deputy's change payloads onto the existing `Roster`/
`Timesheet` models. Depends on characterising Deputy's webhook payload shapes against a
real install first.

## iCal calendar export of your roster

**Status: Planned.**

A read-only `.ics` feed of the signed-in user's upcoming shifts, generated from the
same `get_my_roster` path that already exists. Subscribe to it once in Google/Apple/
Outlook Calendar and your shifts appear alongside the rest of your life, auto-refreshing.

*Why it matters:* most people live in their calendar, not in a chat client. A standing
iCal subscription turns "ask the assistant" into "it's already on my phone." It is also
the lowest-risk way to get roster data out of Deputy and in front of a human — read-only,
no writes, a format every calendar app already speaks.

*Rough shape:* a resource or small CLI subcommand that renders `Roster` records as
`VEVENT`s (start/end in the install timezone, area name in the location field, a stable
`UID` per shift so edits update rather than duplicate). RRULE handling can reuse the
recurrence vocabulary the write layer already understands for unavailability. No new
Deputy endpoints required, so this is largely independent of the smoke-test work.

## Timesheet approval tools (manager mode)

**Status: Planned.**

The current write tools are all "act as me" (claim a shift, clock in/out, set my
unavailability). Manager mode adds the supervisor side: approve/decline submitted
timesheets, approve pay rules, and progress the manager transitions on shift swaps that
`request_shift_swap` today can only *submit* (Pending Approval → Approved/Declined).

*Why it matters:* it turns the server from an employee companion into something a shift
manager can run their day from. Timesheet approval in particular is a repetitive,
end-of-week chore that a well-scoped tool can make far faster.

*Rough shape:* a new group of write tools, gated behind `DEPUTY_ALLOW_WRITES` like the
existing ones and almost certainly behind an additional manager-scoped opt-in, because
approving your own colleagues' time is a higher-blast-radius action than clocking
yourself in. Needs the real permission-failure behaviour (403 body shapes) pinned down
so the tools can fail with an honest "you don't have approval rights here" rather than a
raw error.

## Multi-account / multi-install support

**Status: Planned.**

Right now one server process is bound to exactly one Deputy install: `DeputyConfig` is
frozen, built once from `DEPUTY_*` at startup, and the client caches a single install's
transport. Multi-install support would let one server talk to several installs (e.g. a
franchise operator, or a consultant managing multiple clients) with the target selected
per call.

*Why it matters:* anyone who works across more than one Deputy tenant currently has to
run — and switch between — multiple server instances. One server, many installs, chosen
explicitly per request is far less friction.

*Rough shape:* a registry of named installs (each its own token + base URL), a
per-tool `install` selector argument, and a client factory/pool keyed by install
instead of the current single-client singleton. The token-per-install model is
security-sensitive, so credentials stay referenced by configuration, never echoed. This
also pairs naturally with the OAuth work below, since central-login OAuth hands back
each tenant's own `endpoint`.

## OAuth 2.0 flow

**Status: Planned.**

Today's only auth is a **permanent token** (`DEPUTY_API_TOKEN`) — created once in the
Deputy backend, install-scoped, carrying the generating user's permissions. That is the
right choice for a single-user v1: no handshake, no redirect, no token store. The next
step is Deputy's standard OAuth 2.0 authorization-code flow for multi-user and
distributed installs.

*Why it matters:* a permanent token means each user must hand-mint and paste a
credential. OAuth replaces that with a normal "log in to Deputy, grant access" browser
flow, and — importantly — the token-exchange response hands back the user's own install
base URL (the `endpoint` field), so users no longer have to know or type their
`{install}.{geo}` address. It is also the prerequisite for ever offering this as
anything beyond a self-hosted single-user tool.

*Rough shape (grounded in the auth research):* authorize via
`GET https://once.deputy.com/my/oauth/login` (the central broker), exchange the code at
`POST https://once.deputy.com/my/oauth/access_token`, then refresh against the user's
**own** install at `POST https://{install}.{geo}.deputy.com/oauth/access_token`. The
only documented scope is `longlife_refresh_token`; access tokens live 24h
(`expires_in = 86400`); **refresh rotates the refresh token**, so the new one must be
persisted every cycle or the user is locked out. Over the wire the `Authorization:
Bearer {token}` header is identical to the permanent-token path, so the existing
transport barely changes — the new work is the flow, secure token storage, and refresh
lifecycle. (Embedded/partner OAuth is a separate, agreement-gated flow and is explicitly
out of scope.)

## MCP registry + directory listings

**Status: Planned.**

Getting deputy-mcp listed where people discover MCP servers — the community registries
and client directories — once it is published to PyPI and pushed to GitHub.

*Why it matters:* discoverability. A server nobody can find is a server nobody uses. The
registries are how most people find MCP tools.

*Rough shape:* mostly packaging and metadata work — a clean PyPI release, the GitHub
repo public, a `server.json`/manifest in whatever shape the target registries require,
and submitting listings. This is deliberately gated: no registry claim will be made in
any doc until the package is actually published and the listing actually exists.

## MCPB / DXT desktop bundle for non-technical users

**Status: Planned.**

A one-file desktop bundle (MCPB/DXT) so someone who does not use a terminal can install
deputy-mcp into a desktop MCP client by double-clicking, then fill in their token and
install URL through a form instead of editing JSON config by hand.

*Why it matters:* the current install path (`uvx deputy-mcp`, hand-edited client config,
environment variables) is fine for developers and a wall for everyone else — which is
most shift workers, the actual audience. A bundle with a guided setup is the difference
between "engineers can use this" and "my manager can use this."

*Rough shape:* package the server and its entry point into the bundle format, declare
the `DEPUTY_API_TOKEN` / `DEPUTY_BASE_URL` (and the optional `DEPUTY_ALLOW_WRITES`)
inputs as user-facing config fields with clear help text, and lean on the config layer's
existing fail-closed, "here's exactly which variable to set" error messages to make
setup self-correcting. Depends on nothing but a published package, but pairs well with
the OAuth work (which removes the "type your install URL" step entirely).

## Smoke-test-driven hardening

**Status: Planned.**

The single most important item, and the one the honesty note at the top points to. The
server is validated by an entirely respx-mocked test suite against the *documented* API — it has
never made a real call to a live Deputy install. Several behaviours are documented
ambiguously or not at all, and only first contact with a real tenant will settle them.

*Why it matters:* mocked tests prove the code does what we *think* the API does. They
cannot catch a place where the docs are wrong or silent. Until a real smoke test runs,
each of the gaps below is a known unknown, and no doc should claim the server is
"validated against a live Deputy instance."

*The documented gaps to resolve, from the API research:*

- **Join / association field naming.** The exact field names Deputy returns for joined
  records (e.g. how a roster references its employee/area across `//assoc` and `join`
  query forms) need confirming against real responses; the models assume the documented
  shapes.
- **`/my/*` response envelopes.** The self-scoped endpoints (`my roster`, `my
  timesheets`) may wrap their payloads differently from the `supervise`/`resource`
  endpoints; the real envelope shape needs observing before it can be relied on.
- **Real rate-limit behaviour.** Deputy publishes **no** numeric rate limit, no
  throttling policy, and no rate-limit headers. Third-party clients model `429` +
  `Retry-After`, but that is library behaviour, not vendor-documented. The real throttle
  signal, any `Retry-After`/`RateLimit-*` headers, and `503` overload behaviour must be
  characterised empirically so the retry/backoff logic is tuned to reality rather than a
  guess. (The one hard limit Deputy *does* publish — max 500 records per response — is
  already handled by the pagination path.)
- **Error body shapes.** There is no documented error schema. Expected HTTP semantics
  (401 bad/expired token, 403 insufficient permission, 404 unknown install) are coded
  for, but the actual JSON bodies are unconfirmed; a captured real error is needed to
  pin the parsing.

*Rough shape:* a manual, opt-in smoke-test harness run by a maintainer against their own
install (never in CI, never with committed credentials or real employee data), whose
findings feed back into the models, the error mapping, and the retry logic — and only
then does any "tested against a live install" language become truthful.
