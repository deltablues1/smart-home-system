"""
Configuration module for Google Workspace ADK

Provides agent registry, auth configuration, and MCP server configuration
"""

from .agent_registry import (
    AgentConfig,
    AGENT_REGISTRY,
    get_agent_config,
    get_all_agent_names,
    get_worker_agent_names,
    create_agent_instance,
    generate_orchestrator_routing_guide,
)

from .auth_config import (
    AuthConfig,
    OAuthConfig,
    ServiceAccountConfig,
    get_auth_config,
)

from .deployment_config import (
    DeploymentConfig,
    get_deployment_config,
    is_api_token_required,
    is_telegram_enabled,
    is_wake_word_enabled,
    reset_deployment_config_cache,
)

from .mcp_config import (
    MCPServerConfig,
    MCP_SERVER_REGISTRY,
    get_mcp_server_config,
    get_all_mcp_servers,
)

__all__ = [
    # Agent Registry
    'AgentConfig',
    'AGENT_REGISTRY',
    'get_agent_config',
    'get_all_agent_names',
    'get_worker_agent_names',
    'create_agent_instance',
    'generate_orchestrator_routing_guide',
    # Auth Config
    'AuthConfig',
    'OAuthConfig',
    'ServiceAccountConfig',
    'get_auth_config',
    # Deployment Config
    'DeploymentConfig',
    'get_deployment_config',
    'is_api_token_required',
    'is_telegram_enabled',
    'is_wake_word_enabled',
    'reset_deployment_config_cache',
    # MCP Config
    'MCPServerConfig',
    'MCP_SERVER_REGISTRY',
    'get_mcp_server_config',
    'get_all_mcp_servers',
]
