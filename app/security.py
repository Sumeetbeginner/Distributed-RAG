import hashlib
import secrets
from dataclasses import dataclass
from fastapi import Header, HTTPException, Request
from sqlalchemy import select
from app.db import ApiKey


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue_key(session, tenant_id, label="default", scope="write"):
    raw = "dr_" + secrets.token_urlsafe(32)
    key = ApiKey(tenant_id=tenant_id, digest=digest(raw), label=label, scope=scope)
    session.add(key)
    session.flush()
    return key.id, raw


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    scope: str


def authenticate(request: Request, authorization: str = Header(default="")):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Provide a tenant API key as Bearer token")
    with request.app.state.runtime.session() as session:
        key = session.scalar(select(ApiKey).where(ApiKey.digest == digest(token), ApiKey.revoked.is_(False)))
        if not key:
            raise HTTPException(401, "Invalid or revoked API key")
        principal = Principal(key.tenant_id, key.id, key.scope)
    if not request.app.state.runtime.cache.allow(principal.tenant_id):
        raise HTTPException(429, "Tenant request limit reached", headers={"Retry-After": "60"})
    return principal


def require_write(principal):
    if principal.scope != "write":
        raise HTTPException(403, "This API key is read-only")


def require_admin(request, value):
    expected = request.app.state.runtime.settings.admin_key
    if len(expected) < 24 or not secrets.compare_digest(value, expected):
        raise HTTPException(403, "Admin access unavailable or invalid")
