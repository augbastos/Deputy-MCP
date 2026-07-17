# deputy-mcp — container image running the MCP server over stdio.
FROM python:3.13-slim

# Copy the standalone uv binary from its official image (pinned major version).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

# uv settings: install into the system environment, use copy mode (no hardlinks
# across the layer/volume boundary), and never phone home for Python downloads.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached) from the frozen lockfile, without the
# project itself, so dependency layers survive source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Add the source and install the project (provides the `deputy-mcp` console script).
COPY src ./src
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev

# Put the virtualenv on PATH so the entrypoint resolves without `uv run`.
ENV PATH="/app/.venv/bin:$PATH"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# MCP stdio server: stdout is the protocol channel, so nothing else may write to it.
ENTRYPOINT ["deputy-mcp"]
