# Contributing to deputy-mcp

Thanks for considering a contribution. deputy-mcp is an open-source MCP server for
[Deputy](https://www.deputy.com) (workforce management). This guide covers how to set up
the project, how it is laid out, and the bar a change has to clear before it merges.

The project is deliberately small, strict, and honest: the test suite is green, `ruff`
and `mypy --strict` are clean, and every claim in the docs is backed by what the code
actually does. Contributions are expected to keep it that way.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for dependency management and running
tools. Python **3.11+** is required.

```bash
# Clone, then from the repo root:
uv sync                     # create the venv and install all deps (incl. dev group)

# The three gates a change must pass — all must be green:
uv run pytest               # the full suite (all mocked, no live calls)
uv run ruff check .         # lint
uv run mypy                 # strict type-check (configured over src/)
```

`ruff format .` will apply formatting. `uv run pytest -q` gives a terse run; pass a path
(e.g. `uv run pytest tests/server/test_tools_read.py`) to scope to one module while
iterating.

If all three commands pass locally, CI should pass too — CI runs the same three and
nothing else external (see [Test policy](#test-policy) on why there are no live calls).

## Project layout

The single most important structural rule: **`client/` is MCP-free and reusable;
`server/` is the MCP layer.** The client never imports from the server. Keep it that way
— it is what lets the Deputy client be reused in scripts, a future webhook receiver, or
the CLI without dragging in FastMCP.

```
src/deputy_mcp/
  config.py           # DEPUTY_* env → validated DeputyConfig (fails closed; token is SecretStr)
  oauth.py            # OAuth 2.0 loopback login (`deputy-mcp login`); mints/refreshes tokens
  cli.py              # console entry point
  __main__.py         # `python -m deputy_mcp` / `deputy-mcp` script

  client/             # ── Deputy API client. NO MCP imports. Reusable on its own. ──
    __init__.py       #   DeputyClient (composes the mixins); public models & errors
    http.py           #   DeputyHTTP transport (auth header, retries, error mapping)
    errors.py         #   DeputyError hierarchy (config/auth/permission/rate-limit/...)
    models.py         #   pydantic models (Roster, Timesheet, Employee, ...)
    reads.py          #   ReadsMixin — read methods
    writes.py         #   WritesMixin — mutating methods
    query.py          #   Resource-API query helpers (pagination, //assoc, etc.)
    whoami.py         #   pure accessors over the /api/v1/me (WhoAmI) response
    ical.py           #   personal iCal feed reader (roster-only, token-free mode)

  server/             # ── The MCP layer (FastMCP). Imports from client, never vice-versa. ──
    app.py            #   create_server(): builds one client + FastMCP, wires tools/resources
    tools_read.py     #   the 11 read tools
    _read_helpers.py  #   arg coercion + error formatting shared by the read tools
    tools_write.py    #   the 5 write tools (registered ONLY when DEPUTY_ALLOW_WRITES=true)
    resources.py      #   MCP resources
    prompts.py        #   MCP prompts
    formatting.py     #   markdown/JSON rendering shared by the tools

tests/                # respx-mocked; see Test policy. conftest.py holds the fixtures.
```

A read tool is a thin wrapper: validate/coerce arguments → call one `DeputyClient`
method → render the result via `formatting`. Business logic and API reality live in
`client/`; the tool layer owns argument validation, actionable error text, and
markdown/JSON rendering. Follow that split when adding code — don't put Deputy API
knowledge in `server/`, and don't put MCP concepts in `client/`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
type(optional-scope): short imperative summary
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`. Examples:

```
feat(tools): add deputy_get_leave read tool
fix(client): handle empty //assoc envelope in roster query
docs: clarify permanent-token setup in README
test(writes): cover clock_out with no open timesheet
```

Keep the summary in the imperative mood and under ~72 characters; put the "why" in the
body if it isn't obvious. One logical change per commit.

## Test policy

**Tests are respx-mocked. No test ever makes a live call to Deputy — not locally, not in
CI.** The whole suite runs offline against mocked HTTP responses, and that is a hard
rule, not a convenience.

**Use fictional data only. Never commit real employee data.** The fixtures use invented
people and an invented install — Alex Rivera, Sam O'Brien, Jo Murphy, and the
`cloud-nine-cafe.eu.deputy.com` install — and the token in tests is the literal
placeholder string `test-token-fictional-not-a-secret`. New fixtures must follow suit.

Why this matters, plainly: Deputy records are **colleagues' personal data** — names,
contact details, hours worked, pay. Committing any of it into a public repository is a
privacy breach and, for anyone operating under the GDPR, a legal one. There is no
"anonymised enough" real roster; use the fictional factories. A test that needs a new
shape should extend the payload factories in `tests/conftest.py`, not paste a real API
response.

Practical points:

- Register HTTP routes on the `deputy_api` respx router (bound to the fictional install's
  `/api/v1` base); build payloads with the `make_*` factories in `conftest.py`.
- Every new tool, client method, error path, and rendering branch needs coverage. Bug
  fixes come with a regression test.
- Keep the suite green. Don't skip or `xfail` to get a merge through — if a change makes
  a test fail, either the change or the test is wrong; resolve it.

## Adding a tool

Tools are registered inside the module-level `register(mcp, get_client)` function
(`server/tools_read.py` for reads, `server/tools_write.py` for writes), using the
`@mcp.tool` decorator. Match the existing pattern exactly:

1. **Register with an explicit name and annotations.**

   ```python
   @mcp.tool(name="deputy_get_leave", annotations=read_only)
   async def deputy_get_leave(
       employee: Annotated[str | None, Field(description="Employee name or id.")] = None,
       response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
   ) -> str:
       ...
   ```

   - Name is `deputy_*`, matching the tool family.
   - Read tools use the shared `read_only = {"readOnlyHint": True, "openWorldHint": True}`.
   - Write tools use `_WRITE_ANNOTATIONS` (`readOnlyHint=False`, `destructiveHint=False`,
     `idempotentHint=False`, `openWorldHint=True`) plus a human `title`, and are added to
     `tools_write.py` so they stay gated behind `DEPUTY_ALLOW_WRITES` and are invisible
     when writes are off.
   - Type and document every argument with `Annotated[..., Field(description=...)]`.
     Reuse `_FORMAT_FIELD` for the `response_format` switch.

2. **Honour the docstring contract.** Every tool docstring follows the same three-part
   shape the existing tools use, because that text is what the model reads to decide when
   to call it:
   - A one-line summary, then any argument/default notes.
   - A **"When NOT to use:"** line pointing at the right sibling tool for the adjacent
     job.
   - A **"Returns markdown ... or, with response_format=\"json\", ..."** line describing
     both output shapes.

3. **Keep the tool thin and honest.** Get the client via `get_client()`, call one
   `DeputyClient` method, render with `formatting.render(...)`. Wrap the body in
   `try/except DeputyError` and return the shared error formatter's short, actionable
   string — **never** let a raw traceback reach the model. Put any real API logic in the
   `client/` layer, not the tool.

4. **Don't overclaim in the docstring.** If a capability has a caveat (Deputy has no
   "accept open shift" API, a swap is untargeted, an area must be disambiguated), say so
   in the docstring the way the existing tools do. The tool text is documentation the
   user acts on — it has to be true.

5. **Test it** (see [Test policy](#test-policy)): a `tests/server/` test with mocked
   responses covering the happy path, the error path, and both `markdown`/`json`
   renderings.

## Pull request checklist

Before opening a PR, confirm:

- [ ] `uv run pytest` is green (all tests pass).
- [ ] `uv run ruff check .` is clean.
- [ ] `uv run mypy` is clean (strict).
- [ ] New/changed behaviour has tests, using **fictional data only** — no real employee
      data, no real credentials, no live API calls.
- [ ] New tools follow the register + docstring-contract + annotations pattern, and the
      `client/` (MCP-free) vs `server/` (MCP) split is respected.
- [ ] Docstrings and docs make no claim the code can't back (no "live-tested",
      "production-proven", usage/star counts, or registry claims unless they are actually
      true — see the [ROADMAP](ROADMAP.md) note on the un-run smoke test).
- [ ] Commits follow Conventional Commits.
- [ ] No secrets, tokens, or `.env` files are committed.
- [ ] The PR description says what changed and why.

Small, focused PRs get reviewed faster. If you're planning something large (anything on
the [ROADMAP](ROADMAP.md), for instance), open an issue to discuss the shape first.
