from fastapi import APIRouter

from app.api.v1 import health, tasks

api_v1_router = APIRouter()

# Include sub-routers under API v1 namespace
api_v1_router.include_router(health.router, prefix="/health", tags=["Health Checks"])
api_v1_router.include_router(tasks.router, prefix="/tasks", tags=["Tasks & Workflows"])