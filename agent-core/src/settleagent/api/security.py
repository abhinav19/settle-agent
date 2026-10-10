"""API-key authentication (constant-time compare; the key is never logged)."""

import logging
import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

log = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="Control-plane API key")


async def require_api_key(request: Request, supplied: str | None = Security(_scheme)) -> None:
    expected: str = request.app.state.deps.api_key
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        client = request.client.host if request.client else "unknown"
        log.warning("rejected API request %s %s from %s", request.method, request.url.path, client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
