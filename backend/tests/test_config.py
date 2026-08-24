"""Regression tests for the Phase 0 configuration defects (notes.md §Known defects).

Each test names the defect it pins down. `_env_file=None` keeps the developer's own
.env out of the picture, so these assert the declared defaults rather than the machine.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, unconsumed_env_vars


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestDatabaseUrl:
    """D-001 — a sync DSN reaching the async engine must fail at startup, not mid-request."""

    def test_sync_scheme_is_rejected(self):
        with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
            make(
                DATABASE_URL="postgresql://ai_user:ai_password@localhost:5432/platform_db"
            )

    def test_psycopg2_scheme_is_rejected(self):
        with pytest.raises(ValidationError):
            make(DATABASE_URL="postgresql+psycopg2://u:p@localhost:5432/db")

    def test_asyncpg_scheme_is_accepted(self):
        dsn = "postgresql+asyncpg://u:p@localhost:5432/db"
        assert make(DATABASE_URL=dsn).async_database_url == dsn

    def test_unset_url_is_composed_from_postgres_parts(self):
        settings = make(
            DATABASE_URL=None,
            POSTGRES_USER="postgres",
            POSTGRES_PASSWORD="pw",
            POSTGRES_SERVER="localhost",
            POSTGRES_PORT=5432,
            POSTGRES_DB="agent_platform",
        )
        assert settings.async_database_url == (
            "postgresql+asyncpg://postgres:pw@localhost:5432/agent_platform"
        )

    def test_empty_url_falls_back_to_the_composed_form(self):
        assert make(DATABASE_URL="").async_database_url.startswith(
            "postgresql+asyncpg://"
        )


class TestPerRoleModels:
    """D-002 — per-role model variables must be declared fields, not silently dropped."""

    @pytest.mark.parametrize(
        "field",
        [
            "PLANNER_MODEL",
            "RESEARCHER_MODEL",
            "CODER_MODEL",
            "DEBUGGER_MODEL",
            "EVALUATOR_MODEL",
            "REPORTER_MODEL",
            "USE_DOCKER_SANDBOX",
            "MAX_AGENT_RETRIES",
        ],
    )
    def test_field_is_declared(self, field):
        assert field in Settings.model_fields

    def test_override_takes_effect(self):
        assert make(CODER_MODEL="qwen2.5-coder:32b").CODER_MODEL == "qwen2.5-coder:32b"

    def test_role_lookup_falls_back_to_default_model(self):
        settings = make(DEFAULT_MODEL="llama3.1:8b")
        assert settings.model_for_role("coder") == settings.CODER_MODEL
        assert settings.model_for_role("nonexistent") == "llama3.1:8b"

    def test_small_tier_swaps_untouched_roles(self):
        settings = make(PLUTON_MODEL_TIER="small")
        assert settings.CODER_MODEL == "qwen2.5-coder:3b"
        assert settings.PLANNER_MODEL == "llama3.2:3b"

    def test_small_tier_respects_an_explicit_model(self):
        # Only values left at the standard-tier default are swapped; naming a different
        # model wins over the tier.
        settings = make(PLUTON_MODEL_TIER="small", CODER_MODEL="qwen2.5-coder:32b")
        assert settings.CODER_MODEL == "qwen2.5-coder:32b"

    def test_unknown_platform_variable_is_reported(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SANDBOX_TIMEOUT_SECONDS=20\nPOSTGRES_DB=agent_platform\n")
        assert "SANDBOX_TIMEOUT_SECONDS" in unconsumed_env_vars(env)
        assert "POSTGRES_DB" not in unconsumed_env_vars(env)


class TestServiceUrls:
    """D-003 and D-014 — the two-address services must not be conflated."""

    def test_mlflow_tracking_and_public_urls_are_separate_fields(self):
        assert "MLFLOW_TRACKING_URI" in Settings.model_fields
        assert "MLFLOW_PUBLIC_URL" in Settings.model_fields

    def test_mlflow_default_is_not_the_airplay_port(self):
        # Host 5000 is claimed by AirPlay Receiver on macOS (ADR-015).
        assert ":5000" not in make().MLFLOW_PUBLIC_URL

    def test_ollama_default_is_reachable_from_a_host_process(self):
        # host.docker.internal does not resolve outside a container — D-014.
        assert make().OLLAMA_BASE_URL == "http://localhost:11434"


class TestNetworkPosture:
    """§13.2 — loopback by default, and no LAN exposure without a token."""

    def test_binds_loopback_by_default(self):
        assert make().HOST == "127.0.0.1"

    def test_lan_bind_requires_opt_in(self):
        with pytest.raises(ValidationError, match="PLATFORM_ALLOW_LAN"):
            make(HOST="0.0.0.0")

    def test_lan_bind_requires_a_token(self):
        with pytest.raises(ValidationError, match="PLATFORM_API_TOKEN"):
            make(HOST="0.0.0.0", PLATFORM_ALLOW_LAN=True)

    def test_lan_bind_with_token_is_allowed(self):
        settings = make(
            HOST="0.0.0.0", PLATFORM_ALLOW_LAN=True, PLATFORM_API_TOKEN="t" * 32
        )
        assert settings.HOST == "0.0.0.0"

    def test_production_requires_a_token(self):
        with pytest.raises(ValidationError, match="PLATFORM_API_TOKEN"):
            make(ENVIRONMENT="production")

    def test_production_rejects_the_development_secret_key(self):
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            make(ENVIRONMENT="production", PLATFORM_API_TOKEN="t" * 32)


class TestCorsOrigins:
    """D-007 — an explicit allowlist, parseable from the forms a .env actually holds."""

    def test_default_is_a_single_local_origin(self):
        assert make().CORS_ORIGINS == ["http://localhost:3000"]

    def test_comma_separated_string(self):
        settings = make(CORS_ORIGINS="http://localhost:3000, http://localhost:3001")
        assert settings.CORS_ORIGINS == [
            "http://localhost:3000",
            "http://localhost:3001",
        ]

    def test_json_array_string(self):
        assert make(CORS_ORIGINS='["http://a"]').CORS_ORIGINS == ["http://a"]

    def test_no_wildcard_by_default(self):
        assert "*" not in make().CORS_ORIGINS
