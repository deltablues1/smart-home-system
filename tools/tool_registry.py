"""
Tool Registry Engine

Centralni registry koji mapira tool names na implementacijske funkcije.
Omogućava dinamičko registriranje i izvršavanje tool funkcija.
"""

from typing import Dict, Any, Callable, Optional, List
import logging
from dataclasses import dataclass
import inspect

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Definicija pojedinačnog tool-a"""
    name: str
    function: Callable
    description: str
    parameters: Dict[str, Any]
    requires_auth: bool = True
    auth_type: str = "oauth"  # "oauth" ili "service_account"


class ToolRegistry:
    """
    Centralni registry za tool funkcije

    Omogućava:
    - Registraciju tool funkcija
    - Dinamičko izvršavanje tool-a po imenu
    - Validaciju parametara
    - Error handling
    """

    def __init__(self):
        """Inicijalizacija tool registry-a"""
        self._tools: Dict[str, ToolDefinition] = {}
        logger.info("ToolRegistry initialized")

    def register_tool(
        self,
        name: str,
        function: Callable,
        description: str,
        parameters: Dict[str, Any],
        requires_auth: bool = True,
        auth_type: str = "oauth"
    ) -> None:
        """
        Registrira tool u registry

        Args:
            name: Jedinstveni naziv tool-a (npr. "gmail_search_threads")
            function: Python funkcija koja implementira tool
            description: Opis tool-a
            parameters: JSON schema za parametre
            requires_auth: Da li tool zahtijeva autentifikaciju
            auth_type: Tip autentifikacije ("oauth" ili "service_account")
        """
        if name in self._tools:
            logger.warning(f"Tool '{name}' already registered, overwriting")

        tool_def = ToolDefinition(
            name=name,
            function=function,
            description=description,
            parameters=parameters,
            requires_auth=requires_auth,
            auth_type=auth_type
        )

        self._tools[name] = tool_def
        logger.debug(f"Registered tool: {name}")

    def register_decorator(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        requires_auth: bool = True,
        auth_type: str = "oauth"
    ):
        """
        Decorator za registriranje tool funkcija

        Example:
            @tool_registry.register_decorator(
                name="gmail_search",
                description="Search Gmail",
                parameters={...}
            )
            async def gmail_search(credentials, query: str):
                ...
        """
        def decorator(func: Callable):
            self.register_tool(
                name=name,
                function=func,
                description=description,
                parameters=parameters,
                requires_auth=requires_auth,
                auth_type=auth_type
            )
            return func
        return decorator

    async def execute_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
        credentials: Optional[Any] = None
    ) -> Any:
        """
        Izvršava tool po imenu

        Args:
            tool_name: Naziv tool-a
            args: Argumenti za tool (dictionary)
            credentials: Google OAuth2/Service Account credentials

        Returns:
            Rezultat tool izvršavanja

        Raises:
            ValueError: Ako tool ne postoji
            TypeError: Ako parametri nisu validni
        """
        # Provjeri postoji li tool
        if tool_name not in self._tools:
            raise ValueError(f"Tool '{tool_name}' not found in registry")

        tool_def = self._tools[tool_name]

        # Provjeri autentifikaciju
        if tool_def.requires_auth and credentials is None:
            raise ValueError(f"Tool '{tool_name}' requires authentication but no credentials provided")

        # Validacija parametara
        validated_args = self._validate_args(tool_def, args)

        # Izvršavanje
        try:
            logger.info(f"Executing tool: {tool_name}")
            logger.debug(f"Tool args: {validated_args}")

            # Provjeri je li funkcija async
            # IMPORTANT: Always pass credentials (can be None) as all our tool functions expect it as first parameter
            if inspect.iscoroutinefunction(tool_def.function):
                result = await tool_def.function(credentials=credentials, **validated_args)
            else:
                result = tool_def.function(credentials=credentials, **validated_args)

            logger.info(f"Tool '{tool_name}' executed successfully")
            logger.debug(f"Result: {str(result)[:200]}")
            return result

        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution failed: {e}")
            raise

    def _validate_args(self, tool_def: ToolDefinition, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validira argumente prema tool definiciji

        Args:
            tool_def: Tool definicija
            args: Argumenti za validaciju

        Returns:
            Validirani argumenti

        Raises:
            TypeError: Ako obavezni parametar nedostaje
        """
        parameters = tool_def.parameters
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])

        validated = {}

        # Provjeri obavezne parametre
        for req in required:
            if req not in args:
                raise TypeError(f"Missing required parameter: {req}")
            validated[req] = args[req]

        # Dodaj opcionalne parametre
        for key, value in args.items():
            if key in properties and key not in validated:
                validated[key] = value

        return validated

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """
        Dohvaća tool definiciju

        Args:
            name: Naziv tool-a

        Returns:
            ToolDefinition ili None
        """
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """
        Lista svih registriranih tool-ova

        Returns:
            Lista naziva tool-ova
        """
        return list(self._tools.keys())

    def get_tools_by_category(self, category: str) -> List[str]:
        """
        Dohvaća tool-ove po kategoriji (gmail, drive, calendar, itd.)

        Args:
            category: Kategorija (npr. "gmail", "drive")

        Returns:
            Lista naziva tool-ova
        """
        return [
            name for name in self._tools.keys()
            if name.startswith(f"{category}_")
        ]

    def clear(self) -> None:
        """Briše sve registrirane tool-ove"""
        self._tools.clear()
        logger.info("Tool registry cleared")

    def __len__(self) -> int:
        """Broj registriranih tool-ova"""
        return len(self._tools)

    def __repr__(self) -> str:
        return f"<ToolRegistry(tools={len(self._tools)})>"


# Global singleton instance
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """
    Dohvaća singleton instancu ToolRegistry-a

    Returns:
        ToolRegistry instance
    """
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry


# Helper funkcija za brži pristup
async def execute_tool(
    tool_name: str,
    args: Dict[str, Any],
    credentials: Optional[Any] = None
) -> Any:
    """
    Helper funkcija za izvršavanje tool-a

    Args:
        tool_name: Naziv tool-a
        args: Argumenti
        credentials: Credentials

    Returns:
        Rezultat tool izvršavanja
    """
    registry = get_tool_registry()
    return await registry.execute_tool(tool_name, args, credentials)
