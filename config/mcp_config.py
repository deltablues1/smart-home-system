"""
MCP (Model Context Protocol) Configuration

Konfiguracija svih MCP servera za Google Workspace i eksterne servise
"""

import os
from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class MCPServerConfig:
    """Konfiguracija pojedinačnog MCP servera"""

    name: str  # Jedinstveni naziv servera
    command: str  # Komanda za pokretanje (npr. "npx" ili "python")
    args: List[str]  # Argumenti za komandu
    env: Dict[str, str] = field(default_factory=dict)  # Environment varijable
    description: str = ""  # Opis servera


# ============================================================================
# MCP SERVER REGISTRY
# ============================================================================

def get_gmail_mcp_config() -> MCPServerConfig:
    """Gmail MCP server konfiguracija"""
    return MCPServerConfig(
        name="gmail_mcp",
        command="npx",
        args=[
            "-y",
            "@modelcontextprotocol/server-gmail"
        ],
        env={
            "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
            "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "GOOGLE_OAUTH_REFRESH_TOKEN": os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", ""),
        },
        description="Gmail MCP server for email operations"
    )


def get_drive_mcp_config() -> MCPServerConfig:
    """Google Drive MCP server konfiguracija"""
    return MCPServerConfig(
        name="drive_mcp",
        command="npx",
        args=[
            "-y",
            "@modelcontextprotocol/server-gdrive"
        ],
        env={
            "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
            "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "GOOGLE_OAUTH_REFRESH_TOKEN": os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", ""),
        },
        description="Google Drive MCP server for file operations"
    )


def get_calendar_mcp_config() -> MCPServerConfig:
    """Google Calendar MCP server konfiguracija"""
    return MCPServerConfig(
        name="calendar_mcp",
        command="python",
        args=[
            "-m",
            "tools.mcp_toolsets.calendar_mcp"
        ],
        env={
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        },
        description="Google Calendar MCP server for calendar operations"
    )


def get_contacts_mcp_config() -> MCPServerConfig:
    """Google Contacts MCP server konfiguracija"""
    return MCPServerConfig(
        name="contacts_mcp",
        command="python",
        args=[
            "-m",
            "tools.mcp_toolsets.contacts_mcp"
        ],
        env={
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        },
        description="Google Contacts (People API) MCP server"
    )


def get_tasks_mcp_config() -> MCPServerConfig:
    """Google Tasks MCP server konfiguracija"""
    return MCPServerConfig(
        name="tasks_mcp",
        command="python",
        args=[
            "-m",
            "tools.mcp_toolsets.tasks_mcp"
        ],
        env={
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        },
        description="Google Tasks MCP server"
    )


def get_firecrawl_mcp_config() -> MCPServerConfig:
    """Firecrawl MCP server konfiguracija"""
    return MCPServerConfig(
        name="firecrawl_mcp",
        command="npx",
        args=[
            "-y",
            "@modelcontextprotocol/server-firecrawl"
        ],
        env={
            "FIRECRAWL_API_KEY": os.getenv("FIRECRAWL_API_KEY", ""),
        },
        description="Firecrawl MCP server for web scraping and research"
    )


def get_agentql_mcp_config() -> MCPServerConfig:
    """AgentQL MCP server konfiguracija"""
    return MCPServerConfig(
        name="agentql_mcp",
        command="python",
        args=[
            "-m",
            "tools.mcp_toolsets.agentql_mcp"
        ],
        env={
            "AGENTQL_API_KEY": os.getenv("AGENTQL_API_KEY", ""),
        },
        description="AgentQL MCP server for precise DOM extraction"
    )


# ============================================================================
# MCP REGISTRY - Mapiranje tool names -> MCP config
# ============================================================================

MCP_SERVER_REGISTRY: Dict[str, MCPServerConfig] = {
    "gmail_mcp": get_gmail_mcp_config(),
    "drive_mcp": get_drive_mcp_config(),
    "calendar_mcp": get_calendar_mcp_config(),
    "contacts_mcp": get_contacts_mcp_config(),
    "tasks_mcp": get_tasks_mcp_config(),
    "firecrawl_mcp": get_firecrawl_mcp_config(),
    "agentql_mcp": get_agentql_mcp_config(),
}


def get_mcp_server_config(tool_name: str) -> Optional[MCPServerConfig]:
    """
    Dohvaća MCP server konfiguraciju

    Args:
        tool_name: Naziv alata (npr. "gmail_mcp")

    Returns:
        MCPServerConfig objekt ili None
    """
    return MCP_SERVER_REGISTRY.get(tool_name)


def get_all_mcp_servers() -> List[MCPServerConfig]:
    """
    Dohvaća listu svih MCP server konfiguracija

    Returns:
        Lista MCPServerConfig objekata
    """
    return list(MCP_SERVER_REGISTRY.values())


def get_mcp_server_names() -> List[str]:
    """
    Dohvaća listu svih MCP server naziva

    Returns:
        Lista naziva
    """
    return list(MCP_SERVER_REGISTRY.keys())
