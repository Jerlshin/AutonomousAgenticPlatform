"""Central application settings — the single source of truth for configuration.

Every variable documented in `docs/ARCHITECTURE.md` §14 is declared here as a field.
`.env.example` is *generated* from this class by `make gen-env-example`, so the three
former sources of truth (`Settings`, `.env`, `.env.example`) can no longer diverge
(defect D-012).

Defaults are the **host-development** values: `make migrate` and `make dev` run natively
on the host, so a clean clone with no `.env` at all still points at `localhost` and works
(defects D-001, D-014). Services that run inside `platform_net` receive the in-network
form as a service-level `environment:` override in `infrastructure/docker-compose.yml`;
each such field records that form in `in_network` metadata, which the generator emits as
a comment next to the variable.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

# backend/app/core/config.py -> parents[3] is the repository root, where .env lives.
# Resolving it absolutely matters: `make dev` and `make migrate` both run with the CWD
# set to backend/, so a relative ".env" would silently never be found.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Field groups, in the order the generated .env.example lays them out.
GROUPS: tuple[str, ...] = (
    "Core",
    "Datastores",
    "Models",
    "Sandbox",
    "Graph budgets",
    "Worker and MLflow",
)

# Prefixes owned by this platform. An environment variable starting with one of these
# but matching no field is almost certainly a typo or a stale name, so startup warns
# about it rather than dropping it silently (defect D-002).
KNOWN_ENV_PREFIXES: tuple[str, ...] = (
    "API_",
    "ARTIFACT_",
    "CORS_",
    "DATABASE_",
    "DATASETS_",
    "DEFAULT_",
    "EMBEDDING_",
    "HITL_",
    "LOG_",
    "MAX_",
    "MLFLOW_",
    "OLLAMA_",
    "PLATFORM_",
    "PLUTON_",
    "POSTGRES_",
    "PROJECT_",
    "QDRANT_",
    "REDIS_",
    "RUN_",
    "SANDBOX_",
    "USE_",
    "WORKER_",
)

# Per-role models for the low-resource tier, matching `make pull-models-small`.
SMALL_TIER_MODELS: dict[str, str] = {
    "PLANNER_MODEL": "llama3.2:3b",
    "RESEARCHER_MODEL": "llama3.2:3b",
    "CODER_MODEL": "qwen2.5-coder:3b",
    "DEBUGGER_MODEL": "qwen2.5-coder:3b",
    "EVALUATOR_MODEL": "llama3.2:3b",
    "REPORTER_MODEL": "llama3.2:3b",
    "DEFAULT_MODEL": "llama3.2:3b",
}

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _f(
    default: Any,
    *,
    group: str,
    doc: str,
    in_network: str | None = None,
    secret: bool = False,
    **kwargs: Any,
) -> Any:
    """Declare a settings field carrying the metadata `.env.example` is generated from."""
    extra: dict[str, Any] = {"group": group, "doc": doc}
    if in_network is not None:
        extra["in_network"] = in_network
    if secret:
        extra["secret"] = True
    return Field(default, description=doc, json_schema_extra=extra, **kwargs)


class Settings(BaseSettings):
    """Central application settings powered by Pydantic v2 BaseSettings."""

    model_config = SettingsConfigDict(
        # A CWD-relative .env first (so a backend/.env still works), then the repository
        # root — later files win, which is where the real .env lives.
        env_file=(".env", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `KEY=` in .env means "use the default", not empty string
        extra="ignore",  # unknown vars are warned about at startup, not fatal
        case_sensitive=True,
    )

    # --------------------------------------------------------------------------
    #  Core  (ARCHITECTURE.md §14.1)
    # --------------------------------------------------------------------------
    PROJECT_NAME: str = _f(
        "Autonomous Multi-Agent AI Platform",
        group="Core",
        doc="Human-readable platform name, shown in the OpenAPI title.",
    )
    ENVIRONMENT: Literal["development", "staging", "production", "testing"] = _f(
        "development",
        group="Core",
        doc="development | staging | production | testing.",
    )
    LOG_LEVEL: str = _f("INFO", group="Core", doc="Root log level.")
    LOG_FORMAT: Literal["json", "console"] = _f(
        "json", group="Core", doc="json for shipping, console for local reading."
    )
    API_V1_STR: str = _f("/api/v1", group="Core", doc="Mount prefix for the v1 API.")
    HOST: str = _f(
        "127.0.0.1",
        group="Core",
        doc="Bind address. Loopback by default; LAN binding requires PLATFORM_ALLOW_LAN=1.",
        # S104: this is the *in-network* form, emitted as a comment in .env.example and
        # applied only inside `platform_net`, where the container's own network namespace
        # is the boundary. A host process binding it is refused by
        # `_check_network_exposure` unless PLATFORM_ALLOW_LAN=1 (§13.2).
        in_network="0.0.0.0",  # noqa: S104
    )
    PORT: int = _f(8000, group="Core", doc="API port.")
    PLATFORM_API_TOKEN: str = _f(
        "",
        group="Core",
        doc="Bearer token for every non-health endpoint. Required outside development.",
        secret=True,
    )
    PLATFORM_ALLOW_LAN: bool = _f(
        False,
        group="Core",
        doc="Set to 1 to permit binding a non-loopback HOST.",
    )
    # NoDecode: keep pydantic-settings from JSON-decoding the raw value, so the
    # validator below can accept the comma-separated form .env files actually use.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = _f(
        ["http://localhost:3000"],
        group="Core",
        doc="Comma-separated browser origins allowed to call the API with credentials.",
    )
    SECRET_KEY: str = _f(
        "dev_secret_key_change_in_production",
        group="Core",
        doc="Signing key for session and token material.",
        secret=True,
    )

    # --------------------------------------------------------------------------
    #  Datastores  (ARCHITECTURE.md §14.2)
    # --------------------------------------------------------------------------
    POSTGRES_SERVER: str = _f(
        "localhost", group="Datastores", doc="Postgres host.", in_network="postgres"
    )
    POSTGRES_PORT: int = _f(5432, group="Datastores", doc="Postgres port.")
    POSTGRES_USER: str = _f("postgres", group="Datastores", doc="Postgres role.")
    POSTGRES_PASSWORD: str = _f(
        "postgres_password_dev",
        group="Datastores",
        doc="Postgres password. Must match infrastructure/docker-compose.yml.",
        secret=True,
    )
    POSTGRES_DB: str = _f(
        "agent_platform", group="Datastores", doc="Application database."
    )
    MLFLOW_POSTGRES_DB: str = _f(
        "mlflow",
        group="Datastores",
        doc="MLflow backend store database — separate logical DB, same server.",
    )
    DATABASE_URL: str | None = _f(
        None,
        group="Datastores",
        doc=(
            "Full DSN. If set it MUST use the postgresql+asyncpg:// scheme; anything else "
            "is rejected at startup. Leave unset to compose it from POSTGRES_*."
        ),
    )
    REDIS_URL: str = _f(
        "redis://localhost:6379/0",
        group="Datastores",
        doc="Operational Redis database (queue, streams, locks).",
        in_network="redis://redis:6379/0",
    )
    REDIS_CACHE_URL: str = _f(
        "redis://localhost:6379/1",
        group="Datastores",
        doc="Cache-only Redis database — safe to FLUSHDB.",
        in_network="redis://redis:6379/1",
    )
    QDRANT_URL: str = _f(
        "http://localhost:6333",
        group="Datastores",
        doc="Qdrant REST endpoint.",
        in_network="http://qdrant:6333",
    )
    QDRANT_PREFER_GRPC: bool = _f(
        True, group="Datastores", doc="Use gRPC (port 6334) for bulk ingestion."
    )
    ARTIFACT_INLINE_MAX_BYTES: int = _f(
        262144,
        group="Datastores",
        doc="Artifacts larger than this go to the volume or MLflow instead of a DB column.",
    )

    # --------------------------------------------------------------------------
    #  Models  (ARCHITECTURE.md §14.3)
    # --------------------------------------------------------------------------
    OLLAMA_BASE_URL: str = _f(
        "http://localhost:11434",
        group="Models",
        doc=(
            "Ollama runs natively on the host (ADR-012). host.docker.internal does not "
            "resolve from a host process, so the host form is the default."
        ),
        in_network="http://host.docker.internal:11434",
    )
    PLUTON_MODEL_TIER: Literal["standard", "small"] = _f(
        "standard",
        group="Models",
        doc="small swaps every unset per-role model for the 3B ladder (~6 GB total).",
    )
    PLANNER_MODEL: str = _f("qwen2.5:14b-instruct", group="Models", doc="Planner role.")
    RESEARCHER_MODEL: str = _f("llama3.1:8b", group="Models", doc="Researcher role.")
    CODER_MODEL: str = _f("qwen2.5-coder:7b", group="Models", doc="Coder role.")
    DEBUGGER_MODEL: str = _f("qwen2.5-coder:7b", group="Models", doc="Debugger role.")
    EVALUATOR_MODEL: str = _f("llama3.1:8b", group="Models", doc="Evaluator role.")
    REPORTER_MODEL: str = _f("llama3.1:8b", group="Models", doc="Reporter role.")
    DEFAULT_MODEL: str = _f(
        "llama3.1:8b",
        group="Models",
        doc="Fallback for any role without its own model.",
    )
    EMBEDDING_MODEL: str = _f(
        "nomic-embed-text", group="Models", doc="Embedding model."
    )
    EMBEDDING_DIM: int = _f(
        768, group="Models", doc="Vector width; must match the collection in Qdrant."
    )
    OLLAMA_KEEP_ALIVE: str = _f(
        "30m",
        group="Models",
        doc="How long Ollama keeps a model resident after a call.",
    )
    OLLAMA_REQUEST_TIMEOUT_S: int = _f(
        300, group="Models", doc="Per-request timeout for Ollama calls."
    )

    # --------------------------------------------------------------------------
    #  Sandbox  (ARCHITECTURE.md §14.4)
    # --------------------------------------------------------------------------
    SANDBOX_ENABLED: bool = _f(
        True, group="Sandbox", doc="Master switch for sandboxed code execution."
    )
    USE_DOCKER_SANDBOX: bool = _f(
        True,
        group="Sandbox",
        doc="Use the Docker driver. False selects the in-process stub (development only).",
    )
    SANDBOX_RUNTIME: Literal["runc", "runsc"] = _f(
        "runc", group="Sandbox", doc="runsc selects gVisor where it is installed."
    )
    SANDBOX_DEFAULT_PROFILE: Literal["exec", "train"] = _f(
        "exec", group="Sandbox", doc="Profile used when a node does not name one."
    )
    SANDBOX_IMAGE: str = _f(
        "pluton-sandbox-exec:latest", group="Sandbox", doc="Image for the exec profile."
    )
    SANDBOX_TRAIN_IMAGE: str = _f(
        "pluton-sandbox-train:latest",
        group="Sandbox",
        doc="Image for the train profile.",
    )
    SANDBOX_EXEC_TIMEOUT_S: int = _f(
        60, group="Sandbox", doc="Wall clock for exec runs."
    )
    SANDBOX_TRAIN_TIMEOUT_S: int = _f(
        900, group="Sandbox", doc="Wall clock for train runs."
    )
    SANDBOX_EXEC_MEMORY: str = _f(
        "2g", group="Sandbox", doc="Memory cap, exec profile."
    )
    SANDBOX_TRAIN_MEMORY: str = _f(
        "6g", group="Sandbox", doc="Memory cap, train profile."
    )
    SANDBOX_MAX_OUTPUT_BYTES: int = _f(
        2097152, group="Sandbox", doc="Captured stdout/stderr ceiling per execution."
    )
    DOCKER_HOST: str = _f(
        "unix:///var/run/docker.sock",
        group="Sandbox",
        doc="Docker endpoint the sandbox driver talks to.",
    )
    RUNS_ROOT: str = _f(
        "/runs", group="Sandbox", doc="Mount point of the pluton_runs volume."
    )
    DATASETS_VOLUME: str = _f(
        "pluton_datasets", group="Sandbox", doc="Read-only dataset registry volume."
    )
    DATASETS_ROOT: str = _f(
        "./datasets",
        group="Sandbox",
        doc=(
            "Host-side path to the dataset registry, read by the Planner to bind steps to "
            "concrete datasets. Sandboxes always see it at /datasets via the volume above."
        ),
        in_network="/datasets",
    )

    # --------------------------------------------------------------------------
    #  Graph budgets  (ARCHITECTURE.md §14.5)
    # --------------------------------------------------------------------------
    MAX_DEBUG_ITERATIONS: int = _f(4, group="Graph budgets", doc="Debug loop ceiling.")
    MAX_REPLANS: int = _f(2, group="Graph budgets", doc="Replan ceiling per run.")
    MAX_NODE_VISITS: int = _f(
        60, group="Graph budgets", doc="Total node visits per run."
    )
    MAX_SANDBOX_EXECUTIONS: int = _f(
        12, group="Graph budgets", doc="Sandbox executions per run."
    )
    MAX_AGENT_RETRIES: int = _f(
        2, group="Graph budgets", doc="Retries for a single failing agent call."
    )
    RUN_WALLCLOCK_SECONDS: int = _f(
        1800, group="Graph budgets", doc="Hard run deadline."
    )
    RUN_MAX_TOKENS: int = _f(250000, group="Graph budgets", doc="Token budget per run.")
    HITL_GATE_TIMEOUT_S: int = _f(
        1800, group="Graph budgets", doc="How long a human-approval gate waits."
    )

    # --------------------------------------------------------------------------
    #  Worker and MLflow  (ARCHITECTURE.md §14.6)
    # --------------------------------------------------------------------------
    WORKER_MAX_JOBS: int = _f(2, group="Worker and MLflow", doc="Concurrent arq jobs.")
    WORKER_JOB_TIMEOUT_S: int = _f(
        2400, group="Worker and MLflow", doc="arq job timeout."
    )
    WORKER_HEALTH_PORT: int = _f(
        8001, group="Worker and MLflow", doc="Worker /healthz port."
    )
    RUN_LOCK_TTL_S: int = _f(
        1800, group="Worker and MLflow", doc="Redis run-ownership lock TTL."
    )
    MLFLOW_TRACKING_URI: str = _f(
        "http://localhost:5001",
        group="Worker and MLflow",
        doc=(
            "Where the MLflow client logs to. Host port is 5001 because macOS AirPlay "
            "claims 5000 (ADR-015); in-network callers use http://mlflow:5000."
        ),
        in_network="http://mlflow:5000",
    )
    MLFLOW_PUBLIC_URL: str = _f(
        "http://localhost:5001",
        group="Worker and MLflow",
        doc="Browser-reachable MLflow URL, used only to build clickable links.",
    )
    MLFLOW_EXPERIMENT_PREFIX: str = _f(
        "pluton", group="Worker and MLflow", doc="Prefix for created experiments."
    )
    MLFLOW_REGISTRY_ENABLED: bool = _f(
        True, group="Worker and MLflow", doc="Register models produced by a run."
    )

    # --------------------------------------------------------------------------
    #  Validation
    # --------------------------------------------------------------------------

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _validate_database_url(cls, value: Any) -> Any:
        """Reject any DSN that is not an asyncpg one (defect D-001).

        SQLAlchemy's async engine raises `InvalidRequestError: The asyncio extension
        requires an async driver` deep inside the first request otherwise. Failing at
        startup with the correct form is far cheaper to diagnose.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            return value

        url = value.strip()
        if not url.startswith("postgresql+asyncpg://"):
            scheme = url.split("://", 1)[0] if "://" in url else url
            raise ValueError(
                f"DATABASE_URL uses the '{scheme}://' scheme, which the async engine "
                "cannot drive. Use the asyncpg form:\n"
                "  postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DATABASE\n"
                "or leave DATABASE_URL unset to have it composed from POSTGRES_*."
            )
        return url

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        """Accept `a,b` as well as a JSON array, so .env stays readable."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _apply_model_tier(self) -> Settings:
        """Swap in the small-tier ladder for any per-role model left at its default.

        The comparison is against the declared default rather than `model_fields_set`:
        a generated `.env` restates every default explicitly, so "was it set?" cannot
        tell an untouched value from a deliberate one. Naming a model that differs from
        the standard-tier default always wins over the tier.
        """
        if self.PLUTON_MODEL_TIER != "small":
            return self
        for field, model in SMALL_TIER_MODELS.items():
            if getattr(self, field) == type(self).model_fields[field].get_default():
                object.__setattr__(self, field, model)
        return self

    @model_validator(mode="after")
    def _check_network_exposure(self) -> Settings:
        """Enforce the §13.2 posture: loopback by default, no LAN bind without a token."""
        if self.HOST not in _LOOPBACK_HOSTS and not self.PLATFORM_ALLOW_LAN:
            raise ValueError(
                f"HOST={self.HOST} exposes the API beyond loopback. Set "
                "PLATFORM_ALLOW_LAN=1 to confirm that is intended, or keep HOST=127.0.0.1."
            )
        if self.PLATFORM_ALLOW_LAN and not self.PLATFORM_API_TOKEN:
            raise ValueError(
                "PLATFORM_ALLOW_LAN=1 requires PLATFORM_API_TOKEN to be set. "
                "Run `make init-secrets` to generate one."
            )
        if self.ENVIRONMENT in ("staging", "production"):
            if not self.PLATFORM_API_TOKEN:
                raise ValueError(
                    f"PLATFORM_API_TOKEN is required when ENVIRONMENT={self.ENVIRONMENT}."
                )
            # S105: comparing against the declared default is the check, not a
            # hardcoded credential — this is the code that refuses to start with it.
            if self.SECRET_KEY == "dev_secret_key_change_in_production":  # noqa: S105
                raise ValueError(
                    f"SECRET_KEY still holds its development default while "
                    f"ENVIRONMENT={self.ENVIRONMENT}. Run `make init-secrets`."
                )
        return self

    # --------------------------------------------------------------------------
    #  Derived values
    # --------------------------------------------------------------------------

    @property
    def async_database_url(self) -> str:
        """The asyncpg DSN: DATABASE_URL if given, otherwise composed from POSTGRES_*."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    def model_for_role(self, role: str) -> str:
        """Return the model routed to `role` (planner, coder, ...), else DEFAULT_MODEL."""
        return getattr(self, f"{role.upper()}_MODEL", self.DEFAULT_MODEL)


def unconsumed_env_vars(env_path: Path | None = None) -> list[str]:
    """Names set in the environment or .env that look like ours but match no field.

    `extra="ignore"` means a mistyped or renamed variable is dropped without a word —
    which is how five per-role model settings went unnoticed (defect D-002).
    """
    declared = set(Settings.model_fields)
    candidates: set[str] = set()

    path = env_path or (REPO_ROOT / ".env")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                candidates.add(line.split("=", 1)[0].strip())
    candidates.update(os.environ)

    return sorted(
        name
        for name in candidates
        if name not in declared and name.startswith(KNOWN_ENV_PREFIXES)
    )


def warn_unconsumed_env(log: logging.Logger | None = None) -> list[str]:
    """Log a warning for every platform-looking variable no field consumes."""
    unknown = unconsumed_env_vars()
    target = log or logger
    for name in unknown:
        target.warning(
            "Environment variable %s is set but matches no Settings field — it is being "
            "ignored. Check docs/ARCHITECTURE.md §14 for the current name.",
            name,
        )
    return unknown


@lru_cache
def get_settings() -> Settings:
    """Returns a cached instance of system settings."""
    return Settings()


settings = get_settings()
