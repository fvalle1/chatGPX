# syntax=docker/dockerfile:1
FROM python:3.10-slim

# Static uv binary, no pip bootstrap needed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install dependencies first so this layer is cached across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now bring in the rest of the app, including the vendored ./komootgpx library
# (see README: it's gitignored, not tracked by git, but present in this build
# context and required for gpx_sync.py to import komootgpx.api).
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
