#!/bin/sh

set -e

echo "Testing variables"
echo "$POSTGRES_HOST"
echo "$POSTGRES_PORT"
echo "$POSTGRES_USER"


echo "Running entrypoint.sh"
echo "Waiting for Postgres to be ready..."

until pg_isready \
  -h "$POSTGRES_HOST" \
  -p "$POSTGRES_PORT" \
  -U "$POSTGRES_USER"; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

echo "Postgres available"

echo "Running migrations..."
alembic upgrade head || echo "Error running migrations"
echo "Migrations completed."

echo "Starting the application..."

if [ "$RELOAD" = "true" ]; then
  exec uv run uvicorn main:app \
    --app-dir /app/app/ \
    --host 0.0.0.0 \
    --port 8000 \
    --reload
else
  exec uvicorn main:app \
    --app-dir /app/app/ \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${WORKERS:-1}"
fi
