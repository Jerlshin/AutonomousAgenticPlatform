"""Bearer-token enforcement and WebSocket tickets (ARCHITECTURE.md §13.2, §9.3).

`test_config.py` covers the *configuration* half of §13.2 — loopback by default, no LAN
bind without a token. This module covers the enforcement half: that the token is actually
demanded on every endpoint that is supposed to demand it, and on none that is not.

The route-coverage test is the one that matters most. Authentication declared per-endpoint
is authentication somebody eventually forgets on one new endpoint, and the failure is
silent: the route works, the tests pass, and one path is open. So rather than asserting
"these five endpoints 401", it walks the mounted route table and asserts that *every* v1
route either carries the dependency or appears on an explicit, justified exemption list.
A route added later is included automatically and fails until someone decides which it is.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import redis as redis_layer, security
from app.core.config import settings
from app.core.db import get_db
from app.main import app
from tests.fakes import FakeRedis, run

TOKEN = "t" * 43  # what `secrets.token_urlsafe(32)` produces

# Routes that are deliberately reachable without a token, each for a stated reason.
# Adding to this list is a security decision; the test below makes it an explicit one.
UNAUTHENTICATED_ROUTES = {
    # Liveness and readiness. An orchestrator probing a service it cannot authenticate to
    # is the normal case, and the payload names no run and carries no user content.
    "/api/v1/health",
    "/api/v1/health/deep",
    # The Prometheus scrape endpoint (§12.1). Same reasoning, plus: the scraper is a
    # sibling container with no way to hold a secret that the .env does not also leak.
    "/metrics",
    # Documentation and the root banner.
    "/",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/api/v1/openapi.json",
    # The socket itself, which authenticates by ticket or first frame instead — HTTPBearer
    # cannot read a WebSocket scope. `/api/v1/ws/tickets`, the endpoint that mints the
    # credential, *is* token-protected and is not on this list.
    "/api/v1/ws/runs/{run_id}",
}


@pytest.fixture
def with_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """A configured token, which is what turns authentication on at all."""
    monkeypatch.setattr(settings, "PLATFORM_API_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    redis = FakeRedis()
    monkeypatch.setattr(redis_layer, "get_redis", lambda: redis)
    app.dependency_overrides[get_db] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------------------------
#  Route coverage
# ------------------------------------------------------------------------------------


def _effective_routes():
    """Every mounted route with its prefix resolved and its dependencies combined.

    FastAPI includes sub-routers lazily: `app.routes` holds one opaque node per
    `include_router` call, and the routes underneath it keep their *sub-router* paths
    (`/{task_id}`, not `/api/v1/tasks/{task_id}`) until a request is matched. Walking the
    effective contexts is what turns this into the list an operator would recognise — and
    it is also where router-level `dependencies=[...]` shows up, which is the whole point
    of the check below.
    """
    for route in app.routes:
        expand = getattr(route, "effective_route_contexts", None)
        if expand is None:
            yield route
        else:
            yield from expand()


def _route_path(route) -> str:  # noqa: ANN001 - a route context or a bare route
    nested = getattr(route, "starlette_route", None)
    return getattr(route, "path", "") or getattr(nested, "path", "") or ""


def _protected(route) -> bool:  # noqa: ANN001 - a route context or a bare route
    """Whether `require_token` is in this route's resolved dependency tree."""
    nested = getattr(route, "starlette_route", None)
    dependant = getattr(route, "dependant", None) or getattr(nested, "dependant", None)
    if dependant is None:
        return False
    return any(
        dep.call is security.require_token
        for dep in dependant.dependencies
        if dep.call is not None
    )


def test_every_route_is_either_protected_or_explicitly_exempt() -> None:
    """No endpoint is unauthenticated by accident (§13.2)."""
    unprotected = {
        _route_path(route) for route in _effective_routes() if not _protected(route)
    }
    # Anything left here is open and nobody decided it should be.
    assert unprotected - UNAUTHENTICATED_ROUTES == set()


def test_the_exemption_list_does_not_name_routes_that_no_longer_exist() -> None:
    """A stale exemption is a hole waiting for a path to be re-added under the same name."""
    mounted = {_route_path(route) for route in _effective_routes()}
    assert UNAUTHENTICATED_ROUTES - mounted == set()


def test_the_route_table_is_actually_being_walked() -> None:
    """Guards the two tests above against a walker that silently returns nothing.

    Both of them are set-difference assertions, and an empty set passes either one. This
    is the assertion that says the enumeration found real routes with real prefixes.
    """
    paths = {_route_path(route) for route in _effective_routes()}
    assert "/api/v1/tasks/{task_id}" in paths
    assert "/api/v1/ws/runs/{run_id}" in paths
    assert sum(1 for route in _effective_routes() if _protected(route)) >= 15


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/v1/tasks"),
        ("POST", "/api/v1/tasks"),
        ("GET", f"/api/v1/tasks/{uuid.uuid4()}"),
        ("GET", f"/api/v1/runs/{uuid.uuid4()}"),
        ("POST", f"/api/v1/runs/{uuid.uuid4()}/cancel"),
        ("GET", "/api/v1/corpus/documents"),
        ("POST", "/api/v1/corpus/search"),
        ("GET", "/api/v1/benchmarks"),
        ("POST", "/api/v1/ws/tickets"),
    ],
)
def test_protected_endpoints_refuse_a_missing_token(
    client: TestClient, with_token: str, method: str, path: str
) -> None:
    """401 with a `WWW-Authenticate` header, not 403: the client can fix this one."""
    response = client.request(method, path, json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("presented", ["", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_protected_endpoints_refuse_a_wrong_token(
    client: TestClient, with_token: str, presented: str
) -> None:
    response = client.get(
        "/api/v1/tasks", headers={"Authorization": f"Bearer {presented}"}
    )
    assert response.status_code == 401


def test_health_and_metrics_stay_open(client: TestClient, with_token: str) -> None:
    """The two endpoints §13.2 exempts, asserted rather than assumed."""
    assert client.get("/api/v1/health").status_code == 200

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"pluton_" in response.content


def test_a_tokenless_development_box_requires_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token configured means loopback development, and `make dev` must still work.

    `Settings._check_network_exposure` is what makes this safe: it refuses to construct a
    non-loopback or non-development configuration without a token, so this branch can only
    be reached on a developer's own machine.
    """
    monkeypatch.setattr(settings, "PLATFORM_API_TOKEN", "")
    assert security.auth_required() is False
    assert client.get("/api/v1/health").status_code == 200


# ------------------------------------------------------------------------------------
#  Token comparison
# ------------------------------------------------------------------------------------


def test_token_matches_is_exact(with_token: str) -> None:
    assert security.token_matches(TOKEN) is True
    assert security.token_matches(TOKEN.upper()) is False
    assert security.token_matches(TOKEN + " ") is False
    assert security.token_matches(None) is False
    assert security.token_matches("") is False


def test_token_comparison_is_constant_time() -> None:
    """`hmac.compare_digest`, not `==` — the textbook shape for a timing oracle.

    Asserted structurally rather than by timing: a timing measurement in a unit test is a
    flake generator, while "the module imports hmac and calls compare_digest" is exactly
    the property a future refactor could silently drop.
    """
    import inspect

    source = inspect.getsource(security.token_matches)
    assert "compare_digest" in source
    assert "==" not in source.split('"""')[-1]


# ------------------------------------------------------------------------------------
#  WebSocket tickets  (§9.3)
# ------------------------------------------------------------------------------------


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(redis_layer, "get_redis", lambda: client)
    return client


def test_a_ticket_is_scoped_to_one_run(redis: FakeRedis) -> None:
    """A ticket minted for run A does not open run B's socket."""
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket, ttl = run(security.issue_ticket(run_a))

    assert ttl == redis_layer.TICKET_TTL_S
    assert run(security.authenticate_websocket(run_b, ticket=ticket)) is False


def test_a_ticket_is_single_use(redis: FakeRedis) -> None:
    """`GETDEL` is one atomic command, so two sockets racing cannot both succeed."""
    run_id = str(uuid.uuid4())
    ticket, _ = run(security.issue_ticket(run_id))

    assert run(security.authenticate_websocket(run_id, ticket=ticket)) is True
    assert run(security.authenticate_websocket(run_id, ticket=ticket)) is False


def test_a_ticket_is_consumed_even_when_it_belongs_to_another_run(
    redis: FakeRedis,
) -> None:
    """Otherwise one stolen ticket probes run ids one connection at a time."""
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket, _ = run(security.issue_ticket(run_a))

    assert run(security.authenticate_websocket(run_b, ticket=ticket)) is False
    # Burned by the failed attempt — the legitimate owner cannot use it either.
    assert run(security.authenticate_websocket(run_a, ticket=ticket)) is False


def test_an_unknown_ticket_is_refused(redis: FakeRedis) -> None:
    assert run(security.consume_ticket("never-issued")) is None
    assert run(security.consume_ticket("")) is None
    assert (
        run(security.authenticate_websocket(str(uuid.uuid4()), ticket="nope")) is False
    )


def test_an_unreachable_redis_is_a_failed_auth_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A datastore outage must close the socket, never open it."""

    class BrokenRedis:
        async def getdel(self, _key: str) -> str:
            raise ConnectionError("redis is down")

    monkeypatch.setattr(redis_layer, "get_redis", lambda: BrokenRedis())
    assert run(security.consume_ticket("anything")) is None


def test_a_socket_falls_back_to_the_bearer_token_without_a_ticket(
    redis: FakeRedis, with_token: str
) -> None:
    """§9.3's second mechanism, for clients that can set headers (or send a first frame)."""
    run_id = str(uuid.uuid4())

    assert run(security.authenticate_websocket(run_id, token=TOKEN)) is True
    assert run(security.authenticate_websocket(run_id, token="wrong")) is False
    assert run(security.authenticate_websocket(run_id)) is False
