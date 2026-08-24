"""Aggregates the v1 sub-routers under `settings.API_V1_STR`.

Order matters in exactly one place: `websockets` is mounted at `/ws`, so
`POST /api/v1/ws/tickets` and `WS /api/v1/ws/runs/{run_id}` share a prefix and neither
collides with `/runs/{run_id}` on the `runs` router.
"""

from fastapi import APIRouter

from app.api.v1 import benchmarks, corpus, health, runs, tasks, websockets

api_v1_router = APIRouter()

# Include sub-routers under API v1 namespace
api_v1_router.include_router(health.router, prefix="/health", tags=["Health Checks"])
api_v1_router.include_router(tasks.router, prefix="/tasks", tags=["Tasks & Workflows"])
api_v1_router.include_router(runs.router, prefix="/runs", tags=["Runs"])
api_v1_router.include_router(corpus.router, prefix="/corpus", tags=["Corpus & RAG"])
api_v1_router.include_router(
    benchmarks.router, prefix="/benchmarks", tags=["Benchmarks & KPIs"]
)
api_v1_router.include_router(
    websockets.router, prefix="/ws", tags=["Real-time Streaming"]
)
