# MLflow with a PostgreSQL backend store (ADR-013).
#
# The upstream image ships no PostgreSQL driver, so `--backend-store-uri postgresql://…`
# fails at startup with ModuleNotFoundError. Baking psycopg2 in beats installing it on
# every container start, which would need PyPI reachable at runtime.
FROM ghcr.io/mlflow/mlflow:v2.12.1

RUN pip install --no-cache-dir psycopg2-binary==2.9.9

COPY entrypoint.sh /usr/local/bin/mlflow-entrypoint.sh
RUN chmod +x /usr/local/bin/mlflow-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/mlflow-entrypoint.sh"]
