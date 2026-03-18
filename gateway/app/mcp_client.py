"""Gateway server configuration and MCP registry."""
from typing import Optional, Dict, Any

from app.mcp_config import (
    MCPRegistry,
    AgentRegistry,
    get_default_registry,
)


# Global MCP and agent registries
mcp_registry = get_default_registry()
agent_registry = get_default_registry()


def get_enabled_mcp_servers() -> list[str]:
    """Get list of enabled MCP server names."""
    return mcp_registry.get_enabled_servers()


def get_enabled_agents() -> list[str]:
    """Get list of enabled agent IDs."""
    return agent_registry.get_enabled_agents()


def get_mcp_server_config(name: str) -> Optional[Dict]:
    """Get configuration for a specific MCP server."""
    return mcp_registry.get_server_config(name)


def get_agent_config(agent_id: str) -> Optional[Dict]:
    """Get configuration for a specific agent."""
    return agent_registry.get_agent_config(agent_id)


def enable_mcp_server(name: str) -> None:
    """Enable a registered MCP server."""
    mcp_registry.enable_server(name)


def enable_agent(agent_id: str) -> None:
    """Enable a registered agent."""
    agent_registry.enable_agent(agent_id)


def disable_mcp_server(name: str) -> None:
    """Disable a registered MCP server."""
    mcp_registry.disable_server(name)


def disable_agent(agent_id: str) -> None:
    """Disable a registered agent."""
    agent_registry.disable_agent(agent_id)
