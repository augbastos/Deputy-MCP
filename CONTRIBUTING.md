# Contributing to Deputy MCP

Thanks for considering a contribution. This guide covers setup, how the code is laid
out, and the bar a change has to clear. The project is small and strict on purpose:
the gates below are green on `main`, and every claim in the docs is backed by code or a
test. Changes are expected to keep it that way.

## Setup

Deputy MCP uses [uv](https://docs.astral.sh/uv/) and supports Python 3.11–3.13.

```bash
git clone https://github.com/augbastos/Deputy-MCP.git
cd Deputy-MCP
uv sync                          # creates .venv with runtime and dev dependencies

uv run pytest                    # unit, client, server and agent evals; no network
uv run ruff check .              # lint
uv run ruff format --check .     # formatting (also checks code blocks in Markdown)
uv run mypy                      # strict type checking of src/
```

Useful variations: `uv run pytest -m evals` runs only the agent-interface evals,
`uv run pytest --cov` enforces the coverage floor configured in `pyproject.toml`, and
`uv run ruff format .` fixes formatting. CI runs all of these plus a package build,
`pip-audit`, CodeQL and a gitleaks scan (see `.github/workflows/`).

To try the CLI or the server without a Deputy account, run the fictional demo install:

```bash
uv run python examples/mock_deputy.py
# in another shell
DEPUTY_BASE_URL=http://127.0.0.1:8765 DEPUTY_ALLOW_CUSTOM_HOST=true \
DEPUTY_API_TOKEN=demo-token uv run deputy-mcp next
```

## Layout

The rule that shapes everything: **`client/` knows nothing about MCP; `server/` is the
MCP layer.** The client never imports the server, and a test checks that referencing
the server factory, as the CLI does, does not import FastMCP.

```
src/deputy_mcp/
  config.py            DEPUTY_* environment -> validated DeputyConfig (fails closed)
  errors.py            DeputyError hierarchy (leaf module)
  sanitize.py          redaction of error text shown to models, terminals and users
  token_store.py       OAuth token storage: OS keychain by default, file on request
  oauth.py             OAuth 2.0 authorization-code login with a loopback redirect
  render.py            markdown/JSON rendering shared by the server and the CLI
  cli.py               `deputy-mcp` entry point: serve, read commands, login/logout

  client/              Deputy API client, no MCP imports
    http.py            transport: auth header, idempotency-gated retries, cache, refresh
    reads.py           read methods (self-service /my/* and manager QUERY paths)
    writes.py          write methods, each behind the DEPUTY_ALLOW_WRITES gate
    query.py           Resource QUERY DSL builder and pagination
    models.py          pydantic models for Deputy objects
    whoami.py          accessors over the /me response
    ical.py            personal iCal feed reader (iCal mode)

  server/              MCP layer (FastMCP 4)
    app.py             create_server(): one client, tools, resources, prompts
    tools_read.py      read tools
    tools_write.py     write tools, registered only when writes are enabled
    _confirm.py        elicitation-based confirmation for high-impact writes
    _read_helpers.py   argument parsing and error formatting
    resources.py       weekly roster resources
    prompts.py         prompt templates

tests/
  unit/ client/        mocked with respx; fictional data from conftest.py
  server/              real in-memory FastMCP client against the real server
  server/test_agent_evals.py   what a model sees and triggers, on both protocol eras
  live/                opt-in read-only smoke tests (pytest -m live), never in CI
```

A tool is thin: validate arguments, call one `DeputyClient` method, render the result.
Deputy API knowledge belongs in `client/`; descriptions, validation and rendering belong
in `server/`.

## Tests and data

- Tests never call Deputy. HTTP is mocked with respx on the `deputy_api` router in
  `tests/conftest.py`, and payloads come from the `make_*` factories there.
- **Fictional data only.** Cloud Nine Cafe, Alex Rivera, Sam O'Brien and Jo Murphy do not
  exist; the test token is a placeholder. Deputy records are colleagues' personal data
  (names, hours, pay, contact details), so a real response must never be pasted into a
  fixture, "anonymised" or not.
- The autouse fixtures clear `DEPUTY_*` variables, point `HOME` at a temporary directory
  and replace the keyring backend with an in-memory one, so a developer's own `.env`,
  token file or keychain never leaks into a run.
- New behaviour needs a test for the success path, the error path and both output
  formats. A bug fix comes with a regression test. Tests are not skipped or deleted to
  get a change through.
- Live tests (`uv run pytest -m live`) read real credentials from the environment and
  skip without them. They must stay read-only.

## Adding a tool

1. **Register it with a name, a title and typed annotations.**

   ```python
   @mcp.tool(name="deputy_get_leave", annotations=read_only("My leave"))
   async def deputy_get_leave(
       start_date: Annotated[str | None, Field(description="Start date (ISO).")] = None,
       response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
   ) -> str:
       """List the signed-in user's leave requests in a date range."""
   ```

   Read tools use `read_only(title)`; write tools use `_write_annotations(title)` in
   `tools_write.py`, which keeps them behind `DEPUTY_ALLOW_WRITES`.

2. **Write the description for a model choosing between tools.** The docstring becomes
   the tool description. It states what the tool returns and at which Deputy access
   level it works, has a `When NOT to use:` line naming the sibling tool to use instead,
   and documents both output formats. If the JSON output is an object, list its keys as
   ``` ``{"a", "b"}`` ```: the agent evals check the real output against that contract.

3. **Keep secrets and personal data out of answers.** Catch `DeputyError` and return
   `format_error(exc)` (read tools) or the write tools' formatter, both of which redact.
   JSON goes through `render()`, which projects employee records; do not dump raw
   records that carry contact details.

4. **Decide whether a write needs confirmation.** Use `server/_confirm.confirm()` for an
   action that bypasses a Deputy approval step or acts on other people; say in the
   module docstring why a new write does or does not prompt.

5. **Test it**, including a line in `tests/server/test_agent_evals.py` if the tool is a
   read tool (the eval asserts every read tool is covered).

## Commits and pull requests

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat(tools): …`, `fix(client): …`, `docs: …`), one logical change each, with the
reason in the body when it is not obvious.

`main` is protected: changes arrive through pull requests and merge only when every
required check passes. The pull request template asks whether generative AI was used;
either answer is fine, but the `scpe` check fails if the question is left unanswered.

Before opening a pull request:

- [ ] `pytest`, `ruff check`, `ruff format --check` and `mypy` pass locally.
- [ ] New behaviour has tests with fictional data only, and no live calls.
- [ ] No token, secret, calendar link, `.env` file or real personal data is committed.
- [ ] Docs describe only what the code does; no "live-tested" or usage claims without
      evidence.
- [ ] `CHANGELOG.md` has an entry for user-visible changes.

For anything large, such as an item from the [roadmap](ROADMAP.md), open an issue first.
