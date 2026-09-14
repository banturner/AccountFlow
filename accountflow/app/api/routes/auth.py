"""
Auth endpoints.

Tenant login (JWT):
  POST /api/auth/login    — email + password → access token (body) + refresh cookie
  POST /api/auth/refresh  — HttpOnly refresh cookie → new access token
  POST /api/auth/logout   — clears the refresh cookie

Google OAuth 2.0 flow for Gmail + Calendar access (tenant-authenticated):
  Step 1: POST /api/auth/google/start  → returns Google authorisation URL
  Step 2: Google redirects to GET /api/auth/google/callback?code=...&state=<encrypted>
  Step 3: Exchange code for tokens, encrypt, store in integrations table
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant
from app.config import get_settings
from app.core.logging import get_logger
from app.core.passwords import verify_password
from app.core.security import create_state_token, encrypt_token, verify_state_token
from app.core.tokens import create_token, decode_token
from app.database import bind_tenant, get_db
from app.models.integration import Integration
from app.models.tenant import Tenant

router = APIRouter()
settings = get_settings()
log = get_logger(__name__)

# Pre-computed, valid bcrypt hash of a random throwaway secret. Used to
# equalise login timing when the email is unknown, so response time does
# not reveal which emails are registered. Never matches any real password.
_DUMMY_HASH = "$2b$12$fqO64CgJD1xFsHbfUHZw3OqCljL1/g/nUDSv575q1AsJAEp3gzytO"

# Keep in sync with app/services/gmail.py SCOPES (plus the identity scopes needed
# to resolve which mailbox was connected). See that file for why gmail.readonly
# does not escape the restricted-scope security assessment.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def _build_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=settings.google_redirect_uri,
    )


REFRESH_COOKIE = "refresh_token"


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _issue_tokens(response: Response, tenant: Tenant) -> TokenResponse:
    access_expires = settings.access_token_minutes * 60
    refresh_expires = settings.refresh_token_days * 86400
    access = create_token(str(tenant.id), "access", settings.jwt_secret_key, access_expires)
    refresh = create_token(str(tenant.id), "refresh", settings.jwt_secret_key, refresh_expires)
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh,
        max_age=refresh_expires,
        httponly=True,
        secure=settings.environment != "development",
        samesite="strict",
        path="/api/auth",
    )
    return TokenResponse(access_token=access, expires_in=access_expires)


@router.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Tenant owner login: email + password → JWT access token + refresh cookie."""
    result = await db.execute(select(Tenant).where(Tenant.email == body.email.strip().lower()))
    tenant = result.scalar_one_or_none()

    # bcrypt is CPU-bound — run off the event loop. Verify against a dummy
    # hash when the tenant doesn't exist so response time doesn't reveal
    # which emails are registered.
    hashed = tenant.password_hash if (tenant and tenant.password_hash) else _DUMMY_HASH
    ok = await asyncio.to_thread(verify_password, body.password, hashed)

    if not tenant or not tenant.password_hash or not ok or not tenant.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    log.info("tenant_login", tenant_id=str(tenant.id))
    return _issue_tokens(response, tenant)


@router.post("/auth/refresh", response_model=TokenResponse, tags=["Auth"])
async def refresh_access_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Exchange the HttpOnly refresh cookie for a fresh access token."""
    token = request.cookies.get(REFRESH_COOKIE)
    payload = decode_token(token or "", settings.jwt_secret_key, expected_type="refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    try:
        tenant_id = uuid.UUID(payload.get("sub", ""))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    tenant = await db.get(Tenant, tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return _issue_tokens(response, tenant)


@router.post("/auth/logout", tags=["Auth"])
async def logout(response: Response):
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return {"status": "logged_out"}


@router.post("/auth/google/start", tags=["Auth"])
async def google_oauth_start(
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the Google OAuth authorisation URL for the authenticated tenant.
    The dashboard redirects the browser to it."""
    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        # Encrypted + expiring state token — not a guessable raw tenant_id
        state=create_state_token(str(tenant.id)),
    )
    return {"auth_url": auth_url}


@router.get("/auth/google/callback", tags=["Auth"])
async def google_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),  # encrypted state token carrying the tenant_id
    db: AsyncSession = Depends(get_db),
):
    """Exchange the auth code for tokens and persist them."""
    tenant_str = verify_state_token(state)
    if not tenant_str:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")
    try:
        tenant_id = uuid.UUID(tenant_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    # The verified state token is the only tenant authority on this
    # unauthenticated route, so it also binds the row-level-security context
    # (ADR-001): the integrations upsert below runs under RLS.
    await bind_tenant(db, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    flow = _build_flow()
    # Blocking HTTP calls — keep them off the event loop
    await asyncio.to_thread(flow.fetch_token, code=code)
    creds: Credentials = flow.credentials

    # Resolve which mailbox was connected
    from googleapiclient.discovery import build as google_build

    def _fetch_user_email() -> str:
        oauth2_service = google_build("oauth2", "v2", credentials=creds, cache_discovery=False)
        return oauth2_service.userinfo().get().execute().get("email")

    mailbox_address = await asyncio.to_thread(_fetch_user_email)

    # Upsert the integration
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "google",
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.access_token_enc = encrypt_token(creds.token)
        existing.refresh_token_enc = encrypt_token(creds.refresh_token) if creds.refresh_token else existing.refresh_token_enc
        existing.token_expiry = creds.expiry
        existing.mailbox_address = mailbox_address
        existing.is_active = True
        log.info("google_integration_updated", tenant_id=str(tenant_id))
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider="google",
            mailbox_address=mailbox_address,
            access_token_enc=encrypt_token(creds.token),
            refresh_token_enc=encrypt_token(creds.refresh_token) if creds.refresh_token else None,
            token_expiry=creds.expiry,
        )
        db.add(integration)
        log.info("google_integration_created", tenant_id=str(tenant_id))

    await db.commit()
    return {"status": "connected", "mailbox": mailbox_address}


# ── Microsoft 365 / Outlook ───────────────────────────────────────────────
# Same two-step shape as the Google flow above, and the same encrypted,
# expiring state token — the callback is necessarily unauthenticated, so
# without it anyone holding a tenant id could bind their own mailbox to a
# victim tenant and receive that clinic's mail.

MICROSOFT_SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]


@router.post("/auth/microsoft/start", tags=["Auth"])
async def microsoft_oauth_start(tenant: Tenant = Depends(get_current_tenant)):
    """Return the Microsoft consent URL for the authenticated tenant."""
    if not settings.microsoft_client_id:
        raise HTTPException(status_code=503, detail="Microsoft integration is not configured")

    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": settings.microsoft_client_id,
            "response_type": "code",
            "redirect_uri": settings.microsoft_redirect_uri,
            "response_mode": "query",
            "scope": " ".join(["offline_access"] + MICROSOFT_SCOPES),
            "state": create_state_token(str(tenant.id)),
            # Force the consent screen so a refresh token is always issued.
            "prompt": "consent",
        }
    )
    return {"auth_url": f"{settings.microsoft_authority}/oauth2/v2.0/authorize?{query}"}


@router.get("/auth/microsoft/callback", tags=["Auth"])
async def microsoft_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Exchange the auth code for Graph tokens and persist them encrypted."""
    from datetime import datetime, timedelta, timezone

    tenant_str = verify_state_token(state)
    if not tenant_str:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")
    try:
        tenant_id = uuid.UUID(tenant_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    # As in the Google callback: the state token binds the RLS context.
    await bind_tenant(db, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    from app.services.outlook import exchange_code

    token = await exchange_code(code)
    if not token:
        raise HTTPException(status_code=400, detail="Microsoft rejected the authorisation code")

    # id_token_claims carries the signed-in identity without a second API call.
    claims = token.get("id_token_claims") or {}
    mailbox = claims.get("preferred_username") or claims.get("email") or ""

    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "microsoft",
        )
    )
    existing = result.scalar_one_or_none()
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(token.get("expires_in", 3600)))

    if existing:
        existing.access_token_enc = encrypt_token(token["access_token"])
        if token.get("refresh_token"):
            existing.refresh_token_enc = encrypt_token(token["refresh_token"])
        existing.token_expiry = expiry
        existing.mailbox_address = mailbox or existing.mailbox_address
        existing.is_active = True
        existing.last_poll_error = None
        log.info("microsoft_integration_updated", tenant_id=str(tenant_id))
    else:
        db.add(
            Integration(
                tenant_id=tenant_id,
                provider="microsoft",
                mailbox_address=mailbox,
                access_token_enc=encrypt_token(token["access_token"]),
                refresh_token_enc=(
                    encrypt_token(token["refresh_token"]) if token.get("refresh_token") else None
                ),
                token_expiry=expiry,
            )
        )
        log.info("microsoft_integration_created", tenant_id=str(tenant_id))

    await db.commit()
    return {"status": "connected", "mailbox": mailbox}
