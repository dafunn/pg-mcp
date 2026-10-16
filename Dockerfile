FROM docker.io/library/python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-install-project

COPY server.py ./

# The release version reported to MCP clients, from the build's `git describe`
# against the pg-mcp-v* tags. A build without it reports `dev`.
# Stamp a release with
#     --build-arg PG_MCP_VERSION=$(git describe --tags --match 'pg-mcp-v*' | sed 's/^pg-mcp-v//')
ARG PG_MCP_VERSION=dev
# The RUN uses the arg so the layer cache keys on it; an ENV alone can be
# reused across builds with different values and report a stale version.
RUN printf '%s\n' "${PG_MCP_VERSION}" > /app/VERSION
ENV PG_MCP_VERSION=${PG_MCP_VERSION}

ENV PYTHONUNBUFFERED=1
CMD ["/app/.venv/bin/python", "server.py"]
