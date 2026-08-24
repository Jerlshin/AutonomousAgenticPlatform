#!/usr/bin/env bash
# Create the MLflow backend-store database if it is missing, then start the server.
#
# `postgres` only creates POSTGRES_DB on the very first initialisation of its data
# volume, so an existing volume would never gain the `mlflow` database. Creating it here
# makes the service self-healing on both fresh and pre-existing volumes.
set -euo pipefail

PGHOST="${POSTGRES_SERVER:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-postgres}"
PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
MLFLOW_DB="${MLFLOW_POSTGRES_DB:-mlflow}"
ARTIFACT_ROOT="${MLFLOW_ARTIFACT_ROOT:-/mlflow/artifacts}"

export PGHOST PGPORT PGUSER PGPASSWORD MLFLOW_DB

python - <<'PY'
import os
import sys
import time

import psycopg2
from psycopg2 import sql

params = dict(
    host=os.environ["PGHOST"],
    port=int(os.environ["PGPORT"]),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
    dbname="postgres",
)
target = os.environ["MLFLOW_DB"]

# depends_on: service_healthy already gates this, but a healthy Postgres can still
# refuse a connection for a moment during startup recovery.
last_error = None
for attempt in range(30):
    try:
        conn = psycopg2.connect(**params)
        break
    except psycopg2.OperationalError as exc:
        last_error = exc
        time.sleep(1)
else:
    sys.exit(f"could not reach postgres at {params['host']}:{params['port']}: {last_error}")

conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
    if cur.fetchone():
        print(f"[mlflow-entrypoint] database '{target}' already present")
    else:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        print(f"[mlflow-entrypoint] created database '{target}'")
conn.close()
PY

mkdir -p "${ARTIFACT_ROOT}"

# --serve-artifacts hands clients `mlflow-artifacts:/…` URIs they upload over HTTP, so
# no client needs the artifact volume mounted at an identical path (ADR-013).
exec mlflow server \
  --backend-store-uri "postgresql+psycopg2://${PGUSER}:${PGPASSWORD}@${PGHOST}:${PGPORT}/${MLFLOW_DB}" \
  --artifacts-destination "${ARTIFACT_ROOT}" \
  --serve-artifacts \
  --host 0.0.0.0 \
  --port 5000 \
  --workers "${MLFLOW_WORKERS:-2}"
