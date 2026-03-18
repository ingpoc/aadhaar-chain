"""Agent definitions for aadhaar-chain verification workflow."""
from typing import Optional, List, Dict, Any
from .agents import get_all_agents


def get_agent_registry() -> Dict[str, Dict]:
    """Get registry of all agents and their MCP servers."""
    agents = get_all_agents()
    
    registry = {
        agent.agent_id: {
            "agent": agent,
            "mcp_servers": agent.mcp_servers or [],
            "tool_restrictions": agent.tool_restrictions or {},
        }
        for agent in agents
    }
    
    return registry


def get_enabled_agents() -> List[str]:
    """Get list of enabled agent IDs."""
    return [agent_id for agent_id in get_all_agents()]


def get_agent_mcp_servers(agent_id: str) -> List[str]:
    """Get MCP servers for a specific agent."""
    agent = get_agent_by_id(agent_id)
    if agent:
        return agent.mcp_servers or []
    return []
