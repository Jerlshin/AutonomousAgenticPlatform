"""The arq worker package: job execution and scheduled background work.

* `main.py` — `WorkerSettings`, the entrypoint `arq app.worker.main.WorkerSettings` reads.
* `jobs.py` — `execute_run` / `resume_run`: the lock, the graph, the terminal event.
* `cron.py` — the reapers and `mlflow_backfill`.
* `queue.py` — the *dispatch* side, imported by the API. It deliberately imports nothing
  from `jobs.py`: arq enqueues by function name, so the API process never has to import
  LangGraph, Docker or MLflow to push a job id onto a queue.
"""
