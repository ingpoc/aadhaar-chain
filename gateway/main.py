"""FastAPI gateway for aadhaar-chain identity platform."""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from config import apply_runtime_environment, settings, validate_runtime_storage_config
from app.routes import router as identity_router, identities
from app.agentguard_routes import router as agentguard_router
from app.portfolio_agent_routes import router as portfolio_agent_router
from app.commerce_routes import router as commerce_router
from app.commerce_v1_routes import router as commerce_v1_router
from app.realtime_routes import router as realtime_router
from app.agent_manager import agent_manager
from app.runtime_config import resolve_runtime_policy
from app.session_auth import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    parse_session_token,
    session_user_payload,
)
from app.social_auth_routes import router as social_auth_router
from app.ondc_routes import router as ondc_router
from app.ondc_onboard_routes import router as ondc_onboard_router
from app.ondc_bpp import router as ondc_bpp_router
from app.state_store import load_gateway_state
from app.persistence import ConnectionPool, MigrationRunner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize agents and load persisted state on startup."""
    apply_runtime_environment()
    validate_runtime_storage_config()
    persistence_pool = None
    if os.getenv("DATABASE_URL"):
        persistence_pool = ConnectionPool()
        await persistence_pool.open()
        await MigrationRunner(
            persistence_pool, Path(__file__).parent / "migrations"
        ).apply()
    _app.state.persistence_pool = persistence_pool
    persisted_identities, persisted_verifications = load_gateway_state()
    identities.clear()
    identities.update(persisted_identities)
    agent_manager.verification_records.clear()
    agent_manager.verification_records.update(persisted_verifications)
    runtime_policy = resolve_runtime_policy()
    await agent_manager.initialize_agents()
    if persisted_identities or persisted_verifications:
        print(
            "✓ Loaded persisted AadhaarChain state "
            f"(identities={len(persisted_identities)}, "
            f"verifications={len(persisted_verifications)})"
        )
    if runtime_policy.runtime_available:
        provider = getattr(runtime_policy, "provider", "cursor")
        print(
            f"✓ AadhaarChain agent runtime ready "
            f"(provider={provider}, auth={runtime_policy.auth_mode}, model={runtime_policy.model})"
        )
    else:
        print(
            f"⚠ AadhaarChain agent runtime unavailable: {runtime_policy.blocked_reason}"
        )
    # Free /tmp catalog is empty after spin-down; restore the canonical item when ONDC is on.
    if getattr(settings, "ondc_enabled", False):
        try:
            from app.ondc_bpp import ensure_catalog_marker_item

            ensured = ensure_catalog_marker_item()
            title = (ensured.get("item") or {}).get("title") or "marker"
            created = ensured.get("created")
            print(f"✓ ONDC catalog ensure (created={created}, title={title})")
        except Exception as exc:  # noqa: BLE001 — boot must not die on catalog restore
            print(f"⚠ ONDC catalog ensure skipped: {exc}")
    try:
        yield
    finally:
        if persistence_pool is not None:
            await persistence_pool.close()


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Gateway for AgentGuard authorization and ONDC commerce",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)


# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)


# Include identity router (no prefix, router already has prefix)
app.include_router(identity_router)
app.include_router(social_auth_router)
# Onboard (/ondc/on_subscribe) before Beckn callbacks so subscribe is not ingested as catalog.
app.include_router(ondc_onboard_router)
app.include_router(ondc_bpp_router)
app.include_router(ondc_router)
app.include_router(agentguard_router)
app.include_router(portfolio_agent_router)
app.include_router(commerce_router)
app.include_router(commerce_v1_router)
app.include_router(realtime_router)


# Health check endpoint
@app.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": "1.0.0",
    }


@app.get("/api/health", tags=["health"])
async def api_health_check() -> JSONResponse:
    """Legacy health alias for deployed probes and older consumers."""
    return JSONResponse(content=await health_check())


@app.get("/api/auth/me", tags=["auth"])
async def auth_me(request: Request) -> JSONResponse:
    """Return the authenticated principal from the session cookie."""
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session is None:
        return JSONResponse(
            {
                "success": True,
                "message": "No authenticated identity session.",
                "data": None,
            }
        )

    return JSONResponse(
        {
            "success": True,
            "message": "Authenticated identity session active.",
            "data": session_user_payload(session),
        }
    )


@app.get("/api/auth/validate", tags=["auth"])
async def auth_validate(request: Request) -> JSONResponse:
    """Validate the current session cookie without throwing on missing auth."""
    session = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if session is None:
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "valid": False,
                    "user": None,
                },
            }
        )

    return JSONResponse(
        {
            "success": True,
            "data": {
                "valid": True,
                "user": session_user_payload(session),
            },
        }
    )


@app.post("/api/auth/logout", tags=["auth"])
async def auth_logout() -> JSONResponse:
    """Clear the portfolio SSO session cookie."""
    response = JSONResponse(
        {
            "success": True,
            "message": "Identity session revoked.",
            "data": None,
        }
    )
    clear_session_cookie(response)
    return response


# Root endpoint
@app.get("/", tags=["root"])
async def root() -> JSONResponse:
    """Root endpoint with service information."""
    return {
        "service": settings.app_name,
        "status": "running",
        "docs": "/api/docs",
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
