"""Bearer-token authentication and WebSocket ticket issuance (ARCHITECTURE.md §13.2, §9.3).

v1 has a single shared token, `PLATFORM_API_TOKEN`, and no user model. That is a
deliberate scope decision recorded in §13.1: this is a locally hosted platform bound to
loopback by default, and a per-user identity system would be authentication theatre around
a service whose real boundary is the network interface it binds to.

**Development stays open, and only development.** `Settings._check_network_exposure`
already refuses to start with a non-loopback `HOST` or a non-development `ENVIRONMENT`
unless a token is set, so "no token configured" can only mean "loopback development box".
Requiring a token there would make `make dev` fail out of the box for a clean clone, which
is the same failure mode D-001 and D-014 were about.

**Why tickets exist at all.** The browser `WebSocket` constructor cannot set headers, so
the token would otherwise have to travel in the query string of every connection — where
it lands in access logs, browser history and `Referer`. A ticket is single-use, expires in
60 seconds and is scoped to one run, so the worst case for a leaked one is a replay of a
run the holder could already see.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import redis as redis_layer
from app.core.config import settings

logger = logging.getLogger(__name__)

# `auto_error=False` so a missing header reaches our own check rather than producing a
# 403 with FastAPI's wording — the difference between "no credentials" and "bad
# credentials" is the difference between a 401 the client can fix and one it cannot.
_bearer = HTTPBearer(auto_error=False)


def auth_required() -> bool:
    """Whether a token must be presented. False only on a token-less development box."""
    return bool(settings.PLATFORM_API_TOKEN)


def token_matches(candidate: str | None) -> bool:
    """Constant-time comparison against the configured token.

    `hmac.compare_digest` rather than `==`: the token is a fixed secret compared on every
    request, which is the textbook shape for a timing oracle even if exploiting one over
    a LAN is impractical.
    """
    if not auth_required():
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, settings.PLATFORM_API_TOKEN)


async def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """FastAPI dependency enforcing `Authorization: Bearer {PLATFORM_API_TOKEN}`."""
    if not auth_required():
        return
    presented = credentials.credentials if credentials is not None else None
    if not token_matches(presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ------------------------------------------------------------------------------------
#  WebSocket tickets  (§9.3)
# ------------------------------------------------------------------------------------


async def issue_ticket(run_id: str) -> tuple[str, int]:
    """Mint a single-use, run-scoped ticket. Returns `(ticket, ttl_seconds)`.

    The ticket's *value* in Redis is the run id, which is what makes it run-scoped: a
    ticket minted for run A and presented on run B's socket resolves to A, does not match,
    and is refused. Scoping by convention — trusting the client to use it on the right
    run — would make one ticket a key to every run on the box.
    """
    ticket = secrets.token_urlsafe(32)
    await redis_layer.get_redis().set(
        redis_layer.ticket_key(ticket), str(run_id), ex=redis_layer.TICKET_TTL_S
    )
    return ticket, redis_layer.TICKET_TTL_S


async def consume_ticket(ticket: str) -> str | None:
    """Redeem a ticket, returning the run id it was minted for, or None.

    `GETDEL` is what makes it single-use, and it being one atomic command is what makes
    that true under concurrency: two sockets racing on the same ticket cannot both
    succeed, so a ticket captured from a log cannot be replayed alongside the legitimate
    connection it was issued for.
    """
    if not ticket:
        return None
    try:
        return await redis_layer.get_redis().getdel(redis_layer.ticket_key(ticket))
    except Exception as exc:  # noqa: BLE001 - an unreachable Redis is a failed auth
        logger.warning("Redeeming a WebSocket ticket failed: %s", exc)
        return None


async def authenticate_websocket(
    run_id: str, *, ticket: str | None = None, token: str | None = None
) -> bool:
    """Whether a socket for `run_id` may proceed (§9.3's two accepted mechanisms).

    A ticket is always consumed when one is presented, even if it turns out to belong to
    another run: a ticket that survived a failed attempt would let an attacker probe run
    ids one connection at a time with a single stolen credential.
    """
    if ticket:
        owner = await consume_ticket(ticket)
        return owner is not None and owner == str(run_id)
    if not auth_required():
        return True
    return token_matches(token)


__all__ = [
    "auth_required",
    "authenticate_websocket",
    "consume_ticket",
    "issue_ticket",
    "require_token",
    "token_matches",
]
