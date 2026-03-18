"""MCP Server configuration."""
from typing import Optional, List, Dict
from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """Configuration for individual MCP servers."""
    name: str
    command: str
    args: List[str] = []
    env: Dict[str, str] = {}
    enabled: bool = True


class MCPRegistry(BaseModel):
    """Registry of all MCP servers and their configurations."""
    servers: Dict[str, MCPServerConfig] = {}
    
    def register_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """Register a new MCP server."""
        self.servers[name] = MCPServerConfig(
            name=name,
            command=command,
            args=args or [],
            env=env or {}
        )
    
    def enable_server(self, name: str):
        """Enable a registered MCP server."""
        if name in self.servers:
            self.servers[name].enabled = True
    
    def disable_server(self, name: str):
        """Disable a registered MCP server."""
        if name in self.servers:
            self.servers[name].enabled = False
    
    def get_enabled_servers(self) -> List[str]:
        """Get list of enabled MCP server names."""
        return [name for name, config in self.servers.items() if config.enabled]
    
    def get_server_config(self, name: str) -> Optional[MCPServerConfig]:
        """Get configuration for a specific MCP server."""
        return self.servers.get(name)


class AgentRegistry(BaseModel):
    """Registry of all Claude agents and their configurations."""
    agents: Dict[str, "AgentDefinition"] = {}
    
    def register_agent(self, agent_def: "AgentDefinition"):
        """Register a new agent."""
        self.agents[agent_def.agent_id] = agent_def
    
    def enable_agent(self, agent_id: str):
        """Enable a registered agent."""
        if agent_id in self.agents:
            self.agents[agent_id].enabled = True
    
    def disable_agent(self, agent_id: str):
        """Disable a registered agent."""
        if agent_id in self.agents:
            self.agents[agent_id].enabled = False
    
    def get_enabled_agents(self) -> List[str]:
        """Get list of enabled agent IDs."""
        return [agent_id for agent_id, config in self.agents.items() if config.enabled]
    
    def get_agent_config(self, agent_id: str) -> Optional["AgentDefinition"]:
        """Get configuration for a specific agent."""
        return self.agents.get(agent_id)


# Default MCP server configurations
DEFAULT_MCP_SERVERS = {
    "document-processor": MCPServerConfig(
        name="document-processor",
        command="uv",
        args=[
            "--directory",
            "mcp-servers/document-processor",
            "run",
            "python",
            "server.py"
        ],
        env={
            "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128",  # For Tesseract if needed
        },
    ),
    "pattern-analyzer": MCPServerConfig(
        name="pattern-analyzer",
        command="uv",
        args=[
            "--directory",
            "mcp-servers/pattern-analyzer",
            "run",
            "python",
            "server.py"
        ],
    ),
    "compliance-rules": MCPServerConfig(
        name="compliance-rules",
        command="uv",
        args=[
            "--directory",
            "mcp-servers/compliance-rules",
            "run",
            "python",
            "server.py"
        ],
    ),
}

# Default agent configurations
DEFAULT_AGENTS = {
    "document-validator": {
        "agent_id": "document-validator",
        "name": "Document Validator",
        "enabled": True,
    },
    "fraud-detection": {
        "agent_id": "fraud-detection",
        "name": "Fraud Detection",
        "enabled": True,
    },
    "compliance-monitor": {
        "agent_id": "compliance-monitor",
        "name": "Compliance Monitor",
        "enabled": True,
    },
    "orchestrator": {
        "agent_id": "orchestrator",
        "name": "Orchestrator",
        "enabled": True,
    },
}


def get_default_registry() -> tuple[MCPRegistry, AgentRegistry]:
    """Get default MCP server and agent registries."""
    mcp_registry = MCPRegistry(servers=DEFAULT_MCP_SERVERS)
    agent_registry = AgentRegistry(agents=DEFAULT_AGENTS)
    
    return mcp_registry, agent_registry
