FROM python:3.13-alpine AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=0 \
    UV_PROJECT_ENVIRONMENT=./.venv/ \
    UV_COMPILE_BYTECODE=1

# COPY dependencies first to leverage docker layer cache
COPY ./pyproject.toml ./uv.lock ./

# Installs the dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

# COPY the source code after the depndencies because it's most likely to change
COPY ./app/ ./app/
COPY ./entrypoint.sh ./entrypoint.sh
COPY ./alembic ./alembic/
COPY ./alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.13-alpine AS base

WORKDIR /app

RUN apk add --no-cache postgresql-client

RUN adduser --no-create-home --shell /bin/false --uid 1001 --disabled-password appuser

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/app ./app
COPY --from=builder /app/alembic ./alembic
COPY --from=builder /app/alembic.ini ./alembic.ini
COPY --from=builder /app/entrypoint.sh ./entrypoint.sh

RUN chmod +x ./entrypoint.sh \
  && chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    RELOAD=false

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
