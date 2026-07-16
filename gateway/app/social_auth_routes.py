"""Social + booth principal authentication (AgentGuard host identity adapter).

Production IdP: Auth0 Authorization Code Flow
https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow

Optional legacy: direct Google OAuth. Booth: AUTH_DEMO_CONTINUE (local only).
"""
from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.oauth_state import is_allowed_return_url, mint_oauth_state, parse_oauth_state
from app.session_auth import (
    create_principal_session_token,
    set_session_cookie,
    session_user_payload,
    parse_session_token,
    SESSION_COOKIE_NAME,
)
from config import get_runtime_mode, settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _auth0_configured() -> bool:
    return bool(
        settings.auth0_domain
        and settings.auth0_client_id
        and settings.auth0_client_secret
    )


def _auth0_domain() -> str:
    return (settings.auth0_domain or "").strip().rstrip("/")


def _auth0_redirect_uri() -> str:
    return (
        settings.auth0_redirect_uri
        or f"{settings.public_gateway_url.rstrip('/')}/api/auth/auth0/callback"
    )


def _google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _google_redirect_uri() -> str:
    return (
        settings.google_redirect_uri
        or f"{settings.public_gateway_url.rstrip('/')}/api/auth/google/callback"
    )


def _demo_continue_enabled() -> bool:
    """Booth demo login — forced off in staging/production unless AUTH_DEMO_CONTINUE_FORCE."""
    import os

    mode = get_runtime_mode()
    if mode in ("production", "staging"):
        force = (os.getenv("AUTH_DEMO_CONTINUE_FORCE") or "").strip().lower()
        if force not in ("1", "true", "yes"):
            return False
    return bool(settings.auth_demo_continue)


class DemoContinueBody(BaseModel):
    audience: str = Field(..., min_length=3, max_length=64)
    display_name: Optional[str] = Field(None, max_length=120)


def _issue_session(
    *,
    principal_id: str,
    audience: str,
    identity_provider: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
) -> str:
    return create_principal_session_token(
        principal_id=principal_id,
        audience=audience,
        identity_provider=identity_provider,
        display_name=display_name,
        email=email,
    )


@router.get("/providers")
async def auth_providers() -> JSONResponse:
    return JSONResponse(
        {
            "success": True,
            "data": {
                "auth0": _auth0_configured(),
                "google": _google_configured(),
                "demo_continue": _demo_continue_enabled(),
                "runtime_mode": get_runtime_mode(),
            },
        }
    )


@router.get("/auth0/start")
async def auth0_start(
    return_url: str = Query(..., alias="return"),
    aud: str = Query("ondcbuyer"),
) -> RedirectResponse:
    """Start Auth0 Authorization Code Flow (Regular Web App)."""
    if not _auth0_configured():
        raise HTTPException(status_code=503, detail="Auth0 not configured on gateway.")
    if not is_allowed_return_url(return_url):
        raise HTTPException(status_code=400, detail="return URL is not an allowed origin.")
    state = mint_oauth_state(return_url=return_url, aud=aud)
    domain = _auth0_domain()
    params: dict[str, str] = {
        "client_id": settings.auth0_client_id or "",
        "redirect_uri": _auth0_redirect_uri(),
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    if settings.auth0_audience:
        params["audience"] = settings.auth0_audience
    return RedirectResponse(
        f"https://{domain}/authorize?{urlencode(params)}",
        status_code=302,
    )


@router.get("/auth0/callback")
async def auth0_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse:
    if error:
        detail = error_description or error
        raise HTTPException(status_code=400, detail=f"Auth0 error: {detail}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Invalid Auth0 callback.")
    try:
        meta = parse_oauth_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    domain = _auth0_domain()
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_res = await client.post(
            f"https://{domain}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.auth0_client_id,
                "client_secret": settings.auth0_client_secret,
                "code": code,
                "redirect_uri": _auth0_redirect_uri(),
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        if token_res.status_code >= 400:
            raise HTTPException(status_code=502, detail="Auth0 token exchange failed.")
        tokens = token_res.json()
        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Auth0 token missing access_token.")
        info_res = await client.get(
            f"https://{domain}/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_res.status_code >= 400:
            raise HTTPException(status_code=502, detail="Auth0 userinfo failed.")
        info = info_res.json()

    sub = info.get("sub")
    if not sub:
        raise HTTPException(status_code=502, detail="Auth0 userinfo missing sub.")
    # Normalize Auth0 sub (e.g. google-oauth2|123) into a stable principal id.
    safe_sub = str(sub).replace("|", ":")
    token = _issue_session(
        principal_id=f"principal:auth0:{safe_sub}",
        audience=meta["aud"],
        identity_provider="auth0",
        display_name=info.get("name") or info.get("nickname"),
        email=info.get("email"),
    )
    response = RedirectResponse(meta["return_url"], status_code=302)
    set_session_cookie(response, token)
    return response


@router.get("/google/start")
async def google_start(
    return_url: str = Query(..., alias="return"),
    aud: str = Query("ondcbuyer"),
) -> RedirectResponse:
    if not _google_configured():
        raise HTTPException(status_code=503, detail="Google OAuth not configured on gateway.")
    if not is_allowed_return_url(return_url):
        raise HTTPException(status_code=400, detail="return URL is not an allowed origin.")
    state = mint_oauth_state(return_url=return_url, aud=aud)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH}?{urlencode(params)}", status_code=302)


@router.get("/google/callback")
async def google_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback.")
    try:
        meta = parse_oauth_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_res = await client.post(
            _GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": _google_redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        if token_res.status_code >= 400:
            raise HTTPException(status_code=502, detail="Google token exchange failed.")
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Google token missing access_token.")
        info_res = await client.get(
            _GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_res.status_code >= 400:
            raise HTTPException(status_code=502, detail="Google userinfo failed.")
        info = info_res.json()
    sub = info.get("sub")
    if not sub:
        raise HTTPException(status_code=502, detail="Google userinfo missing sub.")
    token = _issue_session(
        principal_id=f"principal:google:{sub}",
        audience=meta["aud"],
        identity_provider="google",
        display_name=info.get("name"),
        email=info.get("email"),
    )
    response = RedirectResponse(meta["return_url"], status_code=302)
    set_session_cookie(response, token)
    return response


@router.post("/demo-continue")
async def demo_continue_post(body: DemoContinueBody) -> JSONResponse:
    if not _demo_continue_enabled():
        raise HTTPException(status_code=403, detail="Demo continue disabled.")
    principal_id = f"principal:demo:{uuid.uuid4().hex[:16]}"
    token = _issue_session(
        principal_id=principal_id,
        audience=body.audience,
        identity_provider="demo",
        display_name=body.display_name or "Demo User",
    )
    response = JSONResponse(
        {
            "success": True,
            "message": "Demo principal session issued.",
            "data": session_user_payload(
                {
                    "principal_id": principal_id,
                    "identity_provider": "demo",
                    "display_name": body.display_name or "Demo User",
                    "aud": body.audience,
                }
            ),
        }
    )
    set_session_cookie(response, token)
    return response


@router.get("/demo-continue")
async def demo_continue_get(
    aud: str = Query("ondcbuyer"),
    return_url: str = Query("http://127.0.0.1:43102/search", alias="return"),
    display_name: str = Query("Demo User"),
) -> RedirectResponse:
    """Browser-friendly booth login (local / AUTH_DEMO_CONTINUE only)."""
    if not _demo_continue_enabled():
        raise HTTPException(status_code=403, detail="Demo continue disabled.")
    if not is_allowed_return_url(return_url):
        raise HTTPException(status_code=400, detail="return URL is not an allowed origin.")
    principal_id = f"principal:demo:{uuid.uuid4().hex[:16]}"
    token = _issue_session(
        principal_id=principal_id,
        audience=aud,
        identity_provider="demo",
        display_name=display_name,
    )
    response = RedirectResponse(return_url, status_code=302)
    set_session_cookie(response, token)
    return response


@router.get("/session-debug")
async def session_debug(request: Request) -> JSONResponse:
    """Dev helper — do not rely on in production UIs."""
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    return JSONResponse(
        {
            "success": True,
            "data": session_user_payload(session) if session else None,
        }
    )
