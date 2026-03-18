"""FastAPI gateway for aadhaar-chain identity platform."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from config import settings
from app.models import (
    AadhaarVerificationData,
    PanVerificationData,
    VerificationStatus,
    IdentityData,
    VerificationStep,
    ApiResponse,
)
from app.routes import router as identity_router
from app.agent_manager import agent_manager
from app.runtime_config import resolve_runtime_policy


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Gateway for identity & asset tokenization platform",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
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


# Startup event: Initialize agents
@app.on_event("startup")
async def startup_event():
    """Initialize Claude Agent SDK and agents on startup."""
    runtime_policy = resolve_runtime_policy()
    await agent_manager.initialize_agents()
    if runtime_policy.runtime_available:
        print(
            "✓ AadhaarChain Claude Agent runtime ready "
            f"(auth={runtime_policy.auth_mode}, model={runtime_policy.model})"
        )
    else:
        print(
            "⚠ AadhaarChain Claude Agent runtime unavailable: "
            f"{runtime_policy.blocked_reason}"
        )


# Health check endpoint
@app.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": "1.0.0",
    }


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
