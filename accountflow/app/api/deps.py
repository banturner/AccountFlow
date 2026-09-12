"""
API dependencies.

Two auth mechanisms, deliberately separate:

1. Tenant auth (JWT) — `get_current_tenant`. The tenant_id comes ONLY from
   the verified token, never from a query parameter, so a tenant can never
   read another tenant's data regardless of what IDs they pass.

2. Admin auth (X-API-Key) — `require_admin_key`. Internal ops only:
   creating/listing tenants. Never given to customers.
"""
import uuid

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import verify_api_key
from app.core.tokens import decode_token
from app.database import get_db
from app.models.tenant import Tenant

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def require_admin_key(api_key: str = Security(api_key_header)) -> str:
    """Internal ops key — tenant management endpoints only."""
    if not api_key or not verify_api_key(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key",
        )
    return api_key


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Resolve the authenticated tenant from a Bearer access token."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials or not credentials.credentials:
        raise unauthorized

    settings = get_settings()
    payload = decode_token(
        credentials.credentials, settings.jwt_secret_key, expected_type="access"
    )
    if not payload:
        raise unauthorized

    try:
        tenant_id = uuid.UUID(payload.get("sub", ""))
    except ValueError:
        raise unauthorized

    tenant = await db.get(Tenant, tenant_id)
    if not tenant or not tenant.is_active:
        raise unauthorized
    return tenant
