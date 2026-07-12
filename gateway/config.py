"""Configuration management for aadhaar-chain gateway."""
import os
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional, Union


def _default_data_dir() -> str:
    """Render Free: writable ephemeral path. Local: ./data. Env DATA_DIR always wins."""
    if os.environ.get("RENDER", "").lower() in {"true", "1"}:
        return "/tmp/aadharchain-data"
    return "./data"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "aadhaar-chain-gateway"
    debug: bool = False

    # Server
    host: str = "127.0.0.1"
    port: int = 43101

    # CORS (accept both comma-separated string and list)
    cors_origins: Union[str, list[str]] = [
        "http://localhost:43100",
        "http://127.0.0.1:43100",
        "http://localhost:43102",
        "http://127.0.0.1:43102",
        "http://localhost:43103",
        "http://127.0.0.1:43103",
        "http://localhost:43105",
        "http://127.0.0.1:43105",
        "https://aadharcha.in",
        "https://www.aadharcha.in",
        "https://ondcbuyer.aadharcha.in",
        "https://ondcseller.aadharcha.in",
        "https://flatwatch.aadharcha.in",
    ]
    cors_allow_credentials: bool = True
    cors_allow_methods: list[str] = ["*"]
    cors_allow_headers: list[str] = ["*"]

    # Solana (on-chain identity-registry bridge)
    solana_rpc_url: str = "http://127.0.0.1:8899"
    solana_on_chain_enabled: bool = False
    solana_commitment: str = "confirmed"
    solana_idl_path: Optional[str] = None
    identity_registry_program_id: str = "DPW1Ji3XhNb4zAnL9SLq5ZBjmG7ePPegWuocY5VeJLdm"
    verification_oracle_program_id: str = "35h6f6txjVcf8UshEaAm8fki2v1nhRLvRHFGNRwnTMrn"
    credential_manager_program_id: str = "Fib1drk4v1pTPFxVZbvkuFxEUiZ8vXZNJuRq97YUdaG4"
    reputation_engine_program_id: str = "FF1mjZ7WBhrUVq8SG7D3vNfGoTtE7L8mBLsi4efRNN2k"
    staking_manager_program_id: str = "7s3ftYP22nGWxqvc6mDA1U4tRVdtaBJpACfg7GnSdJXH"
    oracle_private_key: Optional[str] = None
    oracle_public_key: Optional[str] = None

    # IPFS
    ipfs_gateway_url: str = "https://ipfs.io/ipfs"

    # Setu.co Aadhaar eKYC (preferred production KYC rail)
    setu_ekyc_enabled: bool = False
    setu_ekyc_base_url: str = "https://dg-sandbox.setu.co"
    setu_ekyc_client_id: Optional[str] = None
    setu_ekyc_client_secret: Optional[str] = None
    setu_ekyc_product_instance_id: Optional[str] = None
    public_gateway_url: str = "http://127.0.0.1:43101"
    public_web_url: str = "http://127.0.0.1:43100"

    # Legacy / MeitY API Setu placeholders (not the active eKYC rail)
    apisetu_client_id: Optional[str] = None
    apisetu_client_secret: Optional[str] = None

    # Cursor SDK agent runtime
    cursor_api_key: Optional[str] = None
    cursor_agent_model: str = "composer-2.5"

    # OpenAI Realtime voice (Buyer M12) — server-side only
    openai_api_key: Optional[str] = None
    openai_realtime_model: str = "gpt-realtime-2.1-mini"

    # Storage — on Render Free prefer /tmp (ephemeral, writable); never paid Disk.
    # Override with DATA_DIR; Dockerfile also sets DATA_DIR=/tmp/aadharchain-data.
    data_dir: str = Field(default_factory=_default_data_dir)
    aadhaar_chain_env: str = "demo"
    trust_store_backend: str = "local_file"
    database_url: Optional[str] = None
    evidence_encryption_key: Optional[str] = None
    evidence_encryption_key_id: str = "local-dev"
    evidence_retention_days: int = 90
    verification_rate_limit_per_minute: int = 20

    # Portfolio SSO session cookies (stateless signed tokens)
    session_secret: str = "aadhaarchain-local-dev-session-secret"
    session_ttl_hours: int = 24

    # Social / demo principal (AgentGuard host identity — not wallet)
    # Auth0 (preferred production IdP) — https://auth0.com/docs Authorization Code Flow
    auth0_domain: Optional[str] = None
    auth0_client_id: Optional[str] = None
    auth0_client_secret: Optional[str] = None
    auth0_audience: Optional[str] = None
    auth0_redirect_uri: Optional[str] = None
    # Legacy direct Google OAuth (optional; prefer Auth0 Google connection)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_redirect_uri: Optional[str] = None
    auth_demo_continue: bool = True

    # ONDC / Beckn (server-side only — never Vite secrets)
    ondc_enabled: bool = False
    ondc_subscriber_id: Optional[str] = None
    ondc_bap_id: Optional[str] = None
    ondc_bap_uri: Optional[str] = None
    ondc_gateway_url: Optional[str] = None
    ondc_registry_url: Optional[str] = None
    ondc_signing_private_key_path: Optional[str] = None
    ondc_encryption_private_key_path: Optional[str] = None
    ondc_unique_key_id: Optional[str] = None
    # Dual NP onboarding hosts (site verification + on_subscribe)
    ondc_registry_env: str = "preprod"  # staging | preprod | prod — portal ACK is PreProd
    ondc_buyer_subscriber_id: Optional[str] = "ondcbuyer.aadharcha.in"
    ondc_seller_subscriber_id: Optional[str] = "ondcseller.aadharcha.in"
    ondc_buyer_keys_dir: Optional[str] = None
    ondc_seller_keys_dir: Optional[str] = None
    # auto | portal | local — auto prefers portal-download PEMs when registry_env=preprod
    ondc_keys_source: str = "auto"
    # Seller BPP (PreProd)
    ondc_bpp_id: Optional[str] = "ondcseller.aadharcha.in"
    ondc_bpp_uri: Optional[str] = "https://ondcseller.aadharcha.in/ondc"
    ondc_seller_unique_key_id: Optional[str] = None
    ondc_seller_signing_private_key_path: Optional[str] = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Parse CORS origins from comma-separated string or list."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()


def get_runtime_mode() -> str:
    """Return normalized AadhaarChain runtime mode."""
    raw_mode = (
        settings.aadhaar_chain_env
        or os.getenv("AADHAAR_CHAIN_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or "demo"
    )
    normalized = raw_mode.strip().lower()
    if normalized in {"prod", "production"}:
        return "production"
    if normalized in {"stage", "staging"}:
        return "staging"
    return "demo"


def validate_runtime_storage_config() -> None:
    """Fail loud when production would use non-production trust storage."""
    backend = (settings.trust_store_backend or "local_file").strip().lower()
    if get_runtime_mode() != "production":
        return

    if backend != "postgres":
        raise RuntimeError(
            "AadhaarChain production mode requires TRUST_STORE_BACKEND=postgres; "
            "the local JSON trust store is for demo and fixture use only."
        )

    if not settings.database_url:
        raise RuntimeError(
            "AadhaarChain production mode requires DATABASE_URL for the PostgreSQL trust store."
        )

    if not settings.evidence_encryption_key:
        raise RuntimeError(
            "AadhaarChain production mode requires EVIDENCE_ENCRYPTION_KEY for encrypted evidence storage."
        )

def apply_runtime_environment() -> None:
    """Propagate runtime settings into environment variables during startup."""
    if settings.cursor_api_key and not os.getenv("CURSOR_API_KEY"):
        os.environ["CURSOR_API_KEY"] = settings.cursor_api_key

    if settings.cursor_agent_model and not os.getenv("CURSOR_AGENT_MODEL"):
        os.environ["CURSOR_AGENT_MODEL"] = settings.cursor_agent_model

    if settings.openai_api_key and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
