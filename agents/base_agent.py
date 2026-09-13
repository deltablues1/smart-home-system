"""
Base Agent Class

[!] DEPRECATION NOTICE [!]
This BaseAgent class is LEGACY and should only be used for:
1. Philosophy agents (Socrates, TerminationChecker) - special case
2. Master Router in main.py - routing only

ALL NEW AGENTS should use Google ADK (agents/adk_agents/) instead.
See agents/adk_agents/orchestrator_adk.py for examples.

For ADK agent development guide, see: docs/guides/ADK_AGENT_DEVELOPMENT.md

---

Zajednička base klasa za legacy agente s dijeljenom logikom
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import logging
import os
import time
import uuid

# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()  # Load .env file before anything else

# Gemini ADK imports
from google import genai
from google.genai import types

# Monitoring imports
from monitoring.logging_config import setup_logging, get_logger_with_context
from monitoring.metrics import AgentMetrics, get_metrics_collector
from config.google_runtime import get_gemini_location

# Error Handling imports
from tools.error_handling.error_classifier import classify_error, ErrorSeverity, ErrorCategory
from tools.error_handling.error_messages import get_error_message

# Setup structured logging with auto Cloud Logging detection
cloud_logging_enabled = setup_logging(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    use_cloud_logging=os.getenv('USE_CLOUD_LOGGING', 'true').lower() == 'true',
    project_id=os.getenv('GOOGLE_CLOUD_PROJECT')
)

logger = logging.getLogger(__name__)

if cloud_logging_enabled:
    logger.info("[OK] Cloud Logging is active - logs are being sent to Google Cloud")
else:
    logger.info("[INFO] Local logging active - Cloud Logging disabled")


class BaseAgent:
    """
    Base klasa za sve agente u sustavu

    Pruža zajedničku funkcionalnost:
    - Učitavanje instrukcija iz .md datoteke
    - Konfiguraciju modela
    - Error handling
    """

    def __init__(
        self,
        name: str,
        model: str,
        instruction_file: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Inicijalizacija base agenta

        Args:
            name: Jedinstveni naziv agenta
            model: Gemini model ("gemini-1.5-flash" ili "gemini-1.5-pro")
            instruction_file: Putanja do .md datoteke s instrukcijama
            config: Dodatna konfiguracija (temperature, max_tokens, etc.)
        """
        self.name = name
        self.model = model
        self.instruction_file = instruction_file
        self.config = config or {}

        # Agent-specific logger for structured logging
        self.logger = logging.getLogger(f"agent.{name}")

        # Učitaj instrukcije ako postoje
        self.instructions = self._load_instructions()

        # Log initialization with structured data
        self.logger.info(
            "Agent initialized",
            extra={
                "agent_name": name,
                "model": model,
                "instruction_file": instruction_file,
                "has_config": bool(config)
            }
        )

    def _load_instructions(self) -> str:
        """
        Učitava instrukcije iz .md datoteke

        Returns:
            Instrukcijski tekst ili default poruka
        """
        if not self.instruction_file:
            return f"You are {self.name}, a specialized AI agent."

        instruction_path = Path(self.instruction_file)

        if not instruction_path.exists():
            logger.warning(f"Instruction file not found: {self.instruction_file}")
            return f"You are {self.name}, a specialized AI agent."

        try:
            with open(instruction_path, 'r', encoding='utf-8') as f:
                instructions = f.read()
            logger.debug(f"Loaded instructions from {self.instruction_file}")
            return instructions
        except Exception as e:
            logger.error(f"Failed to load instructions: {e}")
            return f"You are {self.name}, a specialized AI agent."

    def get_model_config(self) -> Dict[str, Any]:
        """
        Vraća konfiguraciju modela

        Returns:
            Dictionary s model parametrima
        """
        return {
            "model": self.model,
            "temperature": self.config.get("temperature", 0.7),
            "max_tokens": self.config.get("max_tokens", 2048),
            **{k: v for k, v in self.config.items() if k not in ["temperature", "max_tokens"]}
        }

    def format_error_message(self, error: Exception, context: str = "") -> str:
        """
        Formatira poruku o grešci za LLM koristeći novi Error Classifier
        """
        classification = classify_error(error)
        
        # Get localized user message (defaulting to Croatian as per project preference)
        user_msg = get_error_message(classification.user_message_key, language="hr")
        
        error_type = type(error).__name__
        error_msg = str(error)

        message = f"Error occurred: {error_type}"
        if context:
            message += f" in {context}"
        message += f"\nDetails: {error_msg}"
        message += f"\n\nSuggestion: {user_msg}"

        return message

    def get_tools(self) -> List[types.Tool]:
        """
        Vraća listu alata dostupnih agentu

        Subclase moraju implementirati ovu metodu

        Returns:
            Lista Tool objekata
        """
        return []

    async def run_with_fallback(self, user_request: str, context: Optional[str] = None, session_id: Optional[str] = None) -> str:
        """
        Executes the agent with advanced error handling and fallback logic.
        """
        try:
            return await self.run(user_request, context, session_id)
        except Exception as e:
            # Classify the error
            classification = classify_error(e)
            
            # Log the error with classification
            if session_id:
                logger_ctx = get_logger_with_context(f"agent.{self.name}", session_id=session_id)
                logger_ctx.error(
                    f"Agent run failed: {str(e)}",
                    extra={
                        "error_severity": classification.severity.value,
                        "error_category": classification.category.value,
                        "retryable": classification.retryable
                    }
                )
            
            # If it's a transient error, we might want to retry (though run() handles some retries)
            # For now, we return a user-friendly message instead of crashing
            user_msg = get_error_message(classification.user_message_key, language="hr")
            return f"[WARNING] {user_msg} (Error: {type(e).__name__})"

    async def run(self, user_request: str, context: Optional[str] = None, session_id: Optional[str] = None) -> str:
        """
        Izvršava zadatak koristeći Gemini LLM s PRAVIM tool calling execution loopom

        Args:
            user_request: Korisnički zahtjev
            context: Dodatni kontekst (npr. prethodne poruke)
            session_id: Session ID za tracking (auto-generates if not provided)

        Returns:
            Odgovor agenta
        """
        # Generate session ID if not provided
        if not session_id:
            session_id = f"session-{uuid.uuid4().hex[:12]}"

        # Start timing
        start_time = time.time()
        tool_calls_count = 0

        # Create context-aware logger for this session
        session_logger = get_logger_with_context(
            f"agent.{self.name}",
            session_id=session_id,
            agent_name=self.name,
            agent_model=self.model
        )

        # Log request received with session context
        session_logger.info(
            "Agent request received",
            extra={
                "request_preview": user_request[:100],  # First 100 chars
                "has_context": bool(context),
                "event_type": "agent_request_start"
            }
        )

        try:
            # Initialize Gemini client - prefer Vertex AI if configured
            project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
            api_key = os.getenv('GOOGLE_API_KEY')

            if project_id:
                # Use Vertex AI (preferred for production)
                session_logger.debug(f"Using Vertex AI with project: {project_id}")
                client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=get_gemini_location(default="global")
                )
            elif api_key:
                # Fallback to API key mode
                session_logger.debug("Using Google AI API key")
                client = genai.Client(api_key=api_key)
            else:
                error_msg = "Neither GOOGLE_CLOUD_PROJECT nor GOOGLE_API_KEY found in environment"
                session_logger.error(
                    error_msg,
                    extra={"event_type": "agent_config_error"}
                )
                AgentMetrics.record_agent_call(self.name, success=False)
                return f"Error: {error_msg}"

            # Get tools for this agent
            tools = self.get_tools()

            # Build system instruction
            system_instruction = self.instructions
            if context:
                system_instruction += f"\n\nContext:\n{context}"

            # Prepare config
            model_config = self.get_model_config()

            # Conversation history za multi-turn tool calling
            messages = [user_request]
            max_iterations = 15  # Increased from 5 to handle complex workflows (OCR, Drive search retries)

            for iteration in range(max_iterations):
                logger.debug(f"Agent {self.name} iteration {iteration + 1}/{max_iterations}")

                # Generate response with tool calling
                response = client.models.generate_content(
                    model=self.model,
                    contents=messages,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=model_config.get('temperature', 0.7),
                        max_output_tokens=model_config.get('max_tokens', 2048),
                        tools=tools if tools else None
                    )
                )

                # Debug: Log response structure
                if not response.candidates:
                    session_logger.warning("No candidates in response")
                elif not response.candidates[0].content:
                    session_logger.warning(f"No content in candidate. Finish reason: {response.candidates[0].finish_reason if hasattr(response.candidates[0], 'finish_reason') else 'unknown'}")
                elif not response.candidates[0].content.parts:
                    session_logger.warning(f"No parts in content. Content: {response.candidates[0].content}")

                # Check if there are function calls
                has_function_calls = False
                function_responses = []

                if (response.candidates and
                    response.candidates[0].content and
                    response.candidates[0].content.parts):
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, 'function_call') and part.function_call:
                            has_function_calls = True
                            func_call = part.function_call

                            # Log tool execution start
                            session_logger.info(
                                f"🔧 Executing tool: {func_call.name}",
                                extra={
                                    "tool_name": func_call.name,
                                    "tool_args": dict(func_call.args),
                                    "event_type": "tool_execution_start"
                                }
                            )

                            # EXECUTE THE TOOL
                            tool_start_time = time.time()
                            try:
                                tool_result = await self._execute_tool(func_call.name, dict(func_call.args))
                                tool_duration = time.time() - tool_start_time
                                tool_calls_count += 1

                                # Record successful tool call
                                AgentMetrics.record_tool_call(
                                    agent_name=self.name,
                                    tool_name=func_call.name,
                                    success=True
                                )

                                session_logger.info(
                                    f"[OK] Tool {func_call.name} completed",
                                    extra={
                                        "tool_name": func_call.name,
                                        "duration_ms": round(tool_duration * 1000, 2),
                                        "result_preview": str(tool_result)[:200],
                                        "event_type": "tool_execution_success"
                                    }
                                )

                                # Create function response
                                function_responses.append(
                                    types.Part.from_function_response(
                                        name=func_call.name,
                                        response={"result": tool_result}
                                    )
                                )
                            except Exception as tool_error:
                                tool_duration = time.time() - tool_start_time
                                
                                # Classify error for better logging
                                classification = classify_error(tool_error)

                                # Record failed tool call
                                AgentMetrics.record_tool_call(
                                    agent_name=self.name,
                                    tool_name=func_call.name,
                                    success=False
                                )

                                session_logger.error(
                                    f"❌ Tool {func_call.name} failed",
                                    extra={
                                        "tool_name": func_call.name,
                                        "duration_ms": round(tool_duration * 1000, 2),
                                        "error": str(tool_error),
                                        "error_type": type(tool_error).__name__,
                                        "error_severity": classification.severity.value,
                                        "event_type": "tool_execution_error"
                                    }
                                )

                                function_responses.append(
                                    types.Part.from_function_response(
                                        name=func_call.name,
                                        response={"error": str(tool_error)}
                                    )
                                )

                if not has_function_calls:
                    # No more function calls, return final response
                    final_text = []

                    # Check if content and parts exist
                    if (response.candidates and
                        response.candidates[0].content and
                        response.candidates[0].content.parts):
                        for part in response.candidates[0].content.parts:
                            if hasattr(part, 'text') and part.text:
                                final_text.append(part.text)

                    result = "\n".join(final_text) if final_text else response.text if hasattr(response, 'text') else str(response)

                    # Record successful execution
                    duration = time.time() - start_time
                    AgentMetrics.record_agent_call(self.name, success=True)

                    session_logger.info(
                        "Agent request completed successfully",
                        extra={
                            "duration_ms": round(duration * 1000, 2),
                            "duration_seconds": round(duration, 2),
                            "tool_calls_count": tool_calls_count,
                            "response_length": len(result),
                            "response_preview": result[:200],
                            "event_type": "agent_request_success"
                        }
                    )

                    return result

                # Add function responses to conversation
                # Extract function call parts directly (already Part objects)
                function_call_parts = [
                    fc for fc in response.candidates[0].content.parts
                    if hasattr(fc, 'function_call') and fc.function_call
                ]

                messages.append(types.Content(
                    role="model",
                    parts=function_call_parts  # Parts are already function_call Parts
                ))
                messages.append(types.Content(
                    role="user",
                    parts=function_responses
                ))

            # Max iterations reached
            duration = time.time() - start_time
            AgentMetrics.record_agent_call(self.name, success=False)

            session_logger.warning(
                f"Agent {self.name} reached max iterations ({max_iterations})",
                extra={
                    "duration_ms": round(duration * 1000, 2),
                    "duration_seconds": round(duration, 2),
                    "tool_calls_count": tool_calls_count,
                    "max_iterations": max_iterations,
                    "event_type": "agent_max_iterations"
                }
            )
            return "Error: Maximum tool calling iterations reached. The task may be too complex."

        except Exception as e:
            duration = time.time() - start_time
            error_msg = self.format_error_message(e, "agent execution")
            
            # Classify error
            classification = classify_error(e)

            # Record failed execution
            AgentMetrics.record_agent_call(self.name, success=False)

            session_logger.error(
                "Agent execution failed",
                extra={
                    "duration_ms": round(duration * 1000, 2),
                    "duration_seconds": round(duration, 2),
                    "tool_calls_count": tool_calls_count,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "error_severity": classification.severity.value,
                    "event_type": "agent_request_error"
                },
                exc_info=True
            )
            
            # Re-raise to let run_with_fallback handle it, or return error message if called directly
            raise e

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """
        Izvršava tool/funkciju koristeći Tool Registry

        Args:
            tool_name: Naziv tool-a
            args: Argumenti za tool

        Returns:
            Rezultat tool izvršavanja

        Raises:
            Exception: Ako tool execution faila
        """
        try:
            # Import tool registry and initialization
            from tools.tool_registry import get_tool_registry
            from tools.initialize_tools import ensure_tools_initialized
            from tools.google_api_client import create_api_client_auto

            # Ensure tools are initialized (registers all tools if not already done)
            ensure_tools_initialized()

            # Get tool registry
            tool_registry = get_tool_registry()

            # Get credentials for API calls
            credentials = None
            tool_def = tool_registry.get_tool(tool_name)

            if tool_def and tool_def.requires_auth:
                # Create API client to get credentials
                try:
                    api_client = create_api_client_auto()
                    credentials = api_client.credentials
                    logger.debug(f"Using credentials for tool: {tool_name}")
                except Exception as e:
                    logger.error(f"Failed to get credentials for tool {tool_name}: {e}")
                    return f"Error: Authentication failed. Please authenticate using: python tools/oauth_cli.py --auth"

            # Execute tool via registry
            logger.info(f"Executing tool via registry: {tool_name}")
            result = await tool_registry.execute_tool(
                tool_name=tool_name,
                args=args,
                credentials=credentials
            )

            return result

        except ValueError as e:
            # Tool not found in registry
            logger.error(f"Tool {tool_name} not found in registry: {e}")
            return f"Error: Tool '{tool_name}' not found. Available tools: {tool_registry.list_tools() if 'tool_registry' in locals() else 'unknown'}"

        except Exception as e:
            # Other execution errors
            logger.error(f"Tool {tool_name} execution failed: {e}")
            # Re-raise to let the caller handle logging and classification
            raise e

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name='{self.name}', model='{self.model}')>"

