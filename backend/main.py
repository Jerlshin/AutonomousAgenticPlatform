import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.db import check_db_connection

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager that runs startup database checks and shutdown cleanup."""
    logger.info("Initializing Autonomous Multi-Agent AI Platform backend...")

    # Verify database connection on startup
    db_ok = await check_db_connection()
    if db_ok:
        logger.info("PostgreSQL Database connection verified successfully.")
    else:
        logger.warning("PostgreSQL Database is unreachable during startup!")

    yield

    logger.info("Shutting down API server gracefully...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Configure Cross-Origin Resource Sharing (CORS) for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production environment
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API v1 routes
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Root route redirecting developer to interactive OpenAPI documentation."""
    return {"message": "Autonomous Multi-Agent AI Platform API", "docs": "/docs"}