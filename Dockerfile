# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

# Pinned, not :latest: an image built next month must not silently use a
# different uv than the one this lockfile was tested with.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install dependencies first, without the project itself, so this layer
# stays cached across builds where only application code changed.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.12-slim-bookworm

# An unprivileged user. The code stays owned by root and world-readable, so
# a compromised process can run it but cannot rewrite it.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home \
       --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn ragbridge.main:app --host 0.0.0.0 --port 8000"]
