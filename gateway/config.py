"""Configuration management for aadhaar-chain gateway."""
import os
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional, Union


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
    ]
    cors_allow_credentials: bool = True
    cors_allow_methods: list[str] = ["*"]
    cors_allow_headers: list[str] = ["*"]

    # Solana
    solana_rpc_url: str = "http://127.0.0.1:8899"

    # IPFS
    ipfs_gateway_url: str = "https://ipfs.io/ipfs"

    # API Setu
    apisetu_client_id: Optional[str] = None
    apisetu_client_secret: Optional[str] = None

    # Anthropic (Claude Agent SDK)
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    claude_agent_auth_mode: str = "auto"
    claude_agent_allow_local_cli_auth: bool = True
    claude_agent_model: str = "claude-haiku-4-5-20251001"
    claude_code_executable: Optional[str] = None

    # Storage
    data_dir: str = "./data"

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

def apply_runtime_environment() -> None:
    """Propagate runtime settings into environment variables during startup."""
    if settings.anthropic_api_key and not os.getenv("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    if settings.anthropic_base_url and not os.getenv("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url

    if settings.claude_code_executable and not os.getenv("CLAUDE_CODE_EXECUTABLE"):
        os.environ["CLAUDE_CODE_EXECUTABLE"] = settings.claude_code_executable
