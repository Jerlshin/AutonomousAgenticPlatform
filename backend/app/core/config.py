from functools import lru_cache # Least Recently Used (LRU) cache decorator. This caches the parsed 
from typing import Literal # type hint to restrict values to a specific set of string literals
from pydantic import Field # to declare custom metadata, defaults, or validation constraints on schema attributes
# BaseSettings - class for managing environment variables
# SettingsConfigDict - used to configure how .env files are read 
from pydantic_settings import BaseSettings, SettingsConfigDict

# declares the main settings class inheriting from BaseSettings.
class Settings(BaseSettings):
    """Central Application Settings powered by Pydantic v2 BaseSettings."""

    # for loading variables
    model_config = SettingsConfigDict(
        env_file=".env", # default .env file
        env_file_encoding="utf-8", # encoding to read the file
        extra="ignore", # ignore any extra env variables that aren't declared in this class
        case_sensitive=True,
    )

    # Core Application & API
    PROJECT_NAME: str = "Autonomous Multi-Agent AI Platform"
    ENVIRONMENT: Literal["development", "staging", "production", "testing"] = (
        "development"
    )
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev_secret_key_change_in_production" # secret key used to signing JWT tokens or encrypting sessions.
    API_V1_STR: str = "/api/v1"

    # network interface for the uvicorn server
    HOST: str = "0.0.0.0"
    PORT: int = 8000 # network port where uvicorn will listen

    # PostgreSQL Configuration
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres_password_dev"
    POSTGRES_DB: str = "agent_platform"
    DATABASE_URL: str | None = None

    # Method signature returning the final connection string as a string
    @property
    def async_database_url(self) -> str:
        """Constructs or returns the asyncpg PostgreSQL connection string."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return ( # asyncpg
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Redis Configuration
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_URL: str = "redis://localhost:6379/0"

    # Qdrant Vector DB Configuration
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_URL: str = "http://localhost:6333"

    # MLOps & Experiment Tracking
    MLFLOW_TRACKING_URI: str = "http://localhost:5000"

    # Ollama Local LLM Engine
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    DEFAULT_MODEL: str = "llama3"

    # Execution Sandbox Configuration
    SANDBOX_IMAGE: str = "agent-sandbox:latest"
    SANDBOX_TIMEOUT_SECONDS: int = 60


@lru_cache
def get_settings() -> Settings:
    """Returns a cached instance of system settings."""
    return Settings()


settings = get_settings()