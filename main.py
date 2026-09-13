"""
Google Workspace ADK Multi-Agent System
Main Entry Point

Čist main.py koji koristi Modular Registry Pattern za učitavanje agenata
"""

import os
import sys
import asyncio
import logging
import time
import re
from typing import Optional, List
from pathlib import Path

# Force UTF-8 on Windows console so emojis/unicode don't crash print/logging
# (CP1250 console can't encode them). Python 3.7+ supports reconfigure().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Emoji sanitization for Windows console compatibility
def sanitize_emojis(text: str) -> str:
    """
    Remove or replace emojis for Windows console compatibility.
    Windows console (CP1250) cannot display Unicode emojis.
    """
    # Map common emojis to ASCII equivalents
    emoji_map = {
        '🤖': '[BOT]',
        '✅': '[OK]',
        '❌': '[ERROR]',
        '⚠️': '[WARNING]',
        '🔴': '[!]',
        '⭐': '[*]',
        '📌': '[PIN]',
        '📋': '[LIST]',
        '📧': '[EMAIL]',
        '🎯': '[TARGET]',
        '🔍': '[SEARCH]',
        '🏛️': '[CLASSROOM]',
        '🤔': '[THINKING]',
        '👷': '[WORKER]',
        '💾': '[MEMORY]',
    }

    # Replace known emojis
    for emoji, replacement in emoji_map.items():
        text = text.replace(emoji, replacement)

    # Remove any remaining emojis. Ranges are kept narrow on purpose —
    # a broad sweep like U+24C2–U+1F251 would also strip CJK text.
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002600-\U000027BF"  # misc symbols + dingbats
        "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A
        "\U0000FE0F"             # variation selector-16
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)

    return text

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

from config.runtime_patches import apply_runtime_patches
apply_runtime_patches()

# Import configurations
from config.agent_registry import (
    get_worker_agent_names,
    create_agent_instance,
    get_registry_stats
)
from config.auth_config import get_auth_config
from config.deployment_config import get_deployment_config


# Import ADK agents and runner utils
from agents.adk_agents.smart_orchestrator import create_smart_orchestrator
from agents.adk_agents.ask_user_agent import create_ask_user_agent
from agents.adk_agents.runner_utils import RunnerHelper
from agents.adk_agents.plan_execute import run_plan_execute
# Philosophy agents using custom BaseAgent instead of ADK
from agents.philosophy.philosophy_agents_custom import socrates_agent, termination_checker

# Import tool initialization
from tools.initialize_tools import ensure_tools_initialized

# Import scheduler
from interfaces.scheduler_interface import SchedulerInterface
from tools.adk_tools.scheduler_adk_tools import set_scheduler_instance


class WorkspaceADKSystem:
    """
    Google Workspace ADK Multi-Agent System

    Glavni sustav koji upravlja agentima i orchestracijom
    """

    def __init__(self):
        """Inicijalizacija sustava"""
        self.orchestrator = None  # ADK orchestrator agent
        self.orchestrator_helper = None  # RunnerHelper for persistent sessions
        self.scheduler = None  # SchedulerInterface for recurring tasks
        self.socrates = socrates_agent
        self.termination_checker = termination_checker
        self.worker_agents: List = []
        self.auth_config = get_auth_config()
        self.deployment_config = get_deployment_config()

        # Generate unique session ID for this CLI session
        self.session_id = f"cli-session-{int(time.time())}"
        self.user_id = "cli-user"

        # Philosophy routing lives in interfaces.base_interface.looks_philosophical,
        # which matches whole words. The set that used to sit here held bare
        # stems ("etik", "logik") matched as substrings, so "napravi etiketu"
        # and "provjeri logiku" both routed to Socrates and flipped active_mode
        # for the whole process.

        # State
        self.active_mode = "LEGACY" # or "CLASSROOM"

        logger.info("=== Google Workspace ADK Multi-Agent System ===")
        logger.info(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
        logger.info(
            "Deployment profile: %s | Telegram=%s | WakeWord=%s | API token required=%s",
            self.deployment_config.profile,
            self.deployment_config.telegram_enabled,
            self.deployment_config.wake_word_enabled,
            self.deployment_config.api_token_required,
        )

    def initialize_agents(self, start_scheduler: bool = False) -> None:
        """
        Inicijalizira sve agente iz registra

        Koristi Modular Registry Pattern za dinamičko učitavanje

        Args:
            start_scheduler: Ako True, pokreće APScheduler. Default False.
                             CLI poziva s True; web/telegram/testi ostaju na False.
                             Scheduler je dostupan kao zaseban proces (run_scheduler.py).
        """
        logger.info("Initializing agents from registry...")

        try:
            from auth.oauth_manager import get_oauth_manager
            oauth_health = get_oauth_manager().get_auth_health_status()
            logger.info(
                "OAuth health: %s (token_path=%s)",
                oauth_health.get("status"),
                oauth_health.get("token_path"),
            )
        except Exception as e:
            logger.warning(f"OAuth health check failed during startup: {e}")

        # Initialize tool registry first
        logger.info("Ensuring tool registry is initialized...")
        ensure_tools_initialized()

        # Dohvati statistiku registra
        stats = get_registry_stats()
        logger.info(f"Registry stats: {stats}")

        # Učitaj worker agente
        worker_names = get_worker_agent_names()
        logger.info(f"Loading {len(worker_names)} worker agents: {worker_names}")

        for agent_name in worker_names:
            try:
                # Dinamički kreiraj agenta iz registra
                agent = create_agent_instance(agent_name)
                self.worker_agents.append(agent)
                logger.info(f"[OK] Loaded: {agent_name} ({agent.model})")
            except Exception as e:
                logger.error(f"✗ Failed to load {agent_name}: {e}")
                # Nastavi s ostalim agentima

        # Kreiraj Enterprise Orchestration Components
        logger.info("Creating Enterprise Orchestration components...")

        # 1. Decision Validator — without specialist sub_agents it cannot
        # actually validate/delegate anything, so don't construct it and don't
        # claim validation is enabled. Wire real sub_agents here to enable it.
        self.decision_validator = None
        logger.info("  - Decision Validator: skipped (no specialist sub-agents configured)")

        # 2. Ask User Agent (for handling failed conditions)
        logger.info("  - Creating Ask User agent...")
        self.ask_user = create_ask_user_agent(
            model="gemini-3.5-flash"  # Tier 2: GA workhorse
        )
        logger.info("  [OK] Ask User agent created")

        # 3. Smart Orchestrator (main coordinator with ALL agents as sub-agents)
        #    Decision Validator and Ask User will be able to access worker agents
        #    through the orchestrator's agent hierarchy
        logger.info("  - Creating Smart Orchestrator agent...")

        self.orchestrator = create_smart_orchestrator(
            # Env override for Pi/Vertex runtime; GA Flash default — no need for costly Pro
            model=os.getenv(
                "ORCHESTRATOR_MODEL",
                os.getenv("FLASH_MODEL", "gemini-3.5-flash"),
            ),
            worker_agents=self.worker_agents,  # For documentation/routing
            validator_agent=self.decision_validator,  # For documentation
            ask_user_agent=self.ask_user  # For documentation
        )
        logger.info(f"[OK] Smart Orchestrator initialized (enterprise-grade)")
        logger.info(f"  - {len(self.worker_agents)} worker agents")
        logger.info(
            "  - Decision validation: %s",
            "ENABLED" if self.decision_validator else "DISABLED (no sub-agents configured)",
        )

        # Kreiraj RunnerHelper za persistent sessions
        logger.info("Creating RunnerHelper for persistent sessions...")
        self.orchestrator_helper = RunnerHelper(
            agent=self.orchestrator,
            session_id=self.session_id,
            user_id=self.user_id,
            app_name="agents"
        )
        logger.info(f"[OK] RunnerHelper initialized with session '{self.session_id}'")

        # Initialize Scheduler for recurring tasks (only when explicitly requested)
        if start_scheduler:
            logger.info("Initializing Scheduler for recurring tasks...")
            self.scheduler = SchedulerInterface()
            self.scheduler.system = self  # Share the same system instance
            self.scheduler._load_saved_jobs()
            self.scheduler.scheduler.start()
            set_scheduler_instance(self.scheduler)
            logger.info(f"[OK] Scheduler initialized with {len(self.scheduler.config.jobs)} saved jobs")
        else:
            logger.info("Scheduler startup skipped (start_scheduler=False). Use run_scheduler.py for background jobs.")

    async def run_orchestration(
        self, message: str, helper=None, user_id: Optional[str] = None
    ) -> str:
        """Single entry point for orchestrating a user request.

        When USE_PLAN_EXECUTE is enabled, genuine multi-step chains are decomposed
        and executed deterministically step-by-step (plan-execute), while single /
        special requests fall back to the Smart Orchestrator. When the flag is off,
        every request goes straight to the Smart Orchestrator (legacy behavior).

        `helper` is the runner for THIS request, passed in rather than read off
        self. A deferred voice run executes its body long after it was queued,
        and by then `self.orchestrator_helper` belonged to a later turn — in
        another session, possibly for another user. Omitting it keeps the CLI
        working, which really does have exactly one session.
        """
        helper = helper or self.orchestrator_helper
        user_id = user_id or self.user_id
        if os.getenv("USE_PLAN_EXECUTE", "false").lower() == "true":
            return await run_plan_execute(
                message,
                self.worker_agents,
                fallback=helper.run,
                session_id=helper.session_id,
                user_id=user_id,
                record_turn=helper.record_exchange,
            )
        return await helper.run(message)

    def verify_authentication(self) -> bool:
        """
        Provjerava je li autentifikacija konfigurirana

        Returns:
            True ako je autentifikacija dostupna
        """
        logger.info("Verifying authentication...")

        # Provjeri OAuth credentials
        oauth_configured = bool(
            self.auth_config.oauth.client_id and
            self.auth_config.oauth.client_secret
        )

        # Provjeri Service Account credentials
        sa_configured = bool(
            self.auth_config.service_account.credentials_file and
            os.path.exists(self.auth_config.service_account.credentials_file)
        )

        logger.info(f"OAuth configured: {oauth_configured}")
        logger.info(f"Service Account configured: {sa_configured}")

        if not oauth_configured and not sa_configured:
            logger.error("No authentication configured!")
            logger.error("Please set up OAuth 2.0 or Service Account credentials.")
            return False

        return True

    async def run_interactive(self) -> None:
        """
        Pokreće interaktivni CLI loop

        Omogućava korisniku da komunicira s agentima
        """
        logger.info("\n=== Starting Interactive Mode ===")
        logger.info(f"Session ID: {self.session_id}")
        print(sanitize_emojis("💾 Conversation memory enabled - agents remember previous messages!"))
        print("Type 'exit' or 'quit' to stop")
        print("Type 'help' for available commands\n")

        while True:
            try:
                # Dohvati input
                prompt_prefix = "\n[CLASSROOM] You: " if self.active_mode == "CLASSROOM" else "\nYou: "
                user_input = input(prompt_prefix).strip()

                if not user_input:
                    continue

                # Komande
                if user_input.lower() in ['exit', 'quit', 'q']:
                    logger.info("Exiting...")
                    break

                elif user_input.lower() == 'help':
                    self.print_help()
                    continue

                elif user_input.lower() == 'agents':
                    self.print_agents()
                    continue

                elif user_input.lower() == 'status':
                    self.print_status()
                    continue
                    
                elif user_input.lower() == 'leave classroom':
                    self.active_mode = "LEGACY"
                    print("Exiting Philosophy Classroom. Back to normal mode.")
                    continue

                elif user_input.lower() in ('classroom', 'enter classroom'):
                    self.active_mode = "CLASSROOM"
                    print("Entering Philosophy Classroom. Type 'leave classroom' to exit.")
                    continue

                # Procesuiraj zahtjev
                logger.info(f"Processing request: {user_input}")

                # Execute through orchestrator
                print(sanitize_emojis("\n🤖 Processing..."))

                # ROUTING LOGIC
                if self.active_mode == "CLASSROOM":
                    # 1. Run Socrates
                    print(sanitize_emojis("🤔 Socrates is thinking..."))
                    socrates_response = await self.socrates.run_with_fallback(user_input)
                    print(sanitize_emojis(f"\n🏛️ Socrates:\n{socrates_response}"))

                    # 2. Run Checker
                    check_input = f"User input: {user_input}\nSocrates response: {socrates_response}"
                    check_response = await self.termination_checker.run_with_fallback(check_input)

                    if "TERMINATE" in check_response:
                        self.active_mode = "LEGACY"
                        print(sanitize_emojis("\n🏛️ Class dismissed. Returning to normal mode."))

                    result = socrates_response

                else:
                    # Philosophy question -> Socrates for THIS turn only.
                    # Entering the classroom for good is the explicit
                    # 'classroom' command: a keyword that flips active_mode
                    # changes the mode for every channel sharing this system.
                    from interfaces.base_interface import looks_philosophical

                    if looks_philosophical(user_input):
                        print(sanitize_emojis("🤔 Socrates is thinking..."))
                        socrates_response = await self.socrates.run_with_fallback(user_input)
                        print(sanitize_emojis(f"\n🏛️ Socrates:\n{socrates_response}"))
                        result = socrates_response
                    else:
                        # Stay in Legacy - use Smart Orchestrator with AgentTool pattern
                        # Smart Orchestrator now handles ALL workflows:
                        # - Single actions
                        # - Multi-step workflows (calls tools one-by-one)
                        # - Conditional logic (IF-THEN-ELSE)
                        logger.info(sanitize_emojis("📋 Using Smart Orchestrator (AgentTool pattern)"))
                        result = await self.run_orchestration(user_input)
                        print(sanitize_emojis(f"\n✅ Result:\n{result}"))

            except KeyboardInterrupt:
                logger.info("\nInterrupted by user")
                break
            except Exception as e:
                logger.error(f"Error: {e}")

    def print_help(self) -> None:
        """Ispisuje help poruku"""
        print("\n=== Available Commands ===")
        print("  help      - Show this help message")
        print("  agents    - List all available agents")
        print("  status    - Show system status")
        print("  exit/quit - Exit the system")
        print("  leave classroom - Force exit from classroom mode")
        print(sanitize_emojis("\n💾 Conversation Memory:"))
        print("  Agents remember all previous messages in this session!")
        print("  You can refer back to earlier parts of the conversation.")
        print(sanitize_emojis("\n⏰ Scheduler:"))
        print("  Schedule recurring tasks using natural language!")
        print("  Example: 'Zakaži svaki petak u 9h slanje weekly reporta na email'")
        print("\nOr simply type your request (e.g., 'Send an email to john@example.com')")

    def print_agents(self) -> None:
        """Ispisuje listu agenata"""
        print("\n=== Available Agents ===")
        print(sanitize_emojis(f"\n🎯 Smart Orchestrator (Enterprise-Grade): {self.orchestrator.name}"))
        print(f"   Model: {self.orchestrator.model}")
        print(f"   Role: Main router with conditional logic validation")
        print(f"   Features: Multi-agent coordination, precondition checking, error handling")

        print(sanitize_emojis(f"\n🔍 Enterprise Orchestration Components:"))
        if self.decision_validator:
            print(f"   - Decision Validator ({self.decision_validator.model})")
            print(f"     |- Validates preconditions (IF-THEN-ELSE logic)")
        else:
            print("   - Decision Validator: disabled (no specialist sub-agents)")
        print(f"   - Ask User Agent ({self.ask_user.model})")
        print(f"     |- Handles failed conditions with user alternatives")

        print(sanitize_emojis(f"\n🏛️ Philosophy Classroom"))
        print(f"   - Socrates ({self.socrates.model})")
        print(f"   - TerminationChecker ({self.termination_checker.model})")

        print(sanitize_emojis(f"\n👷 Worker Agents ({len(self.worker_agents)}):"))
        for agent in self.worker_agents:
            print(f"   - {agent.name} ({agent.model})")

    def print_status(self) -> None:
        """Ispisuje status sustava"""
        stats = get_registry_stats()
        print("\n=== System Status ===")
        print(f"Active Mode: {self.active_mode}")
        print(f"Session ID: {self.session_id}")
        print(f"User ID: {self.user_id}")
        print(sanitize_emojis(f"💾 Conversation Memory: ENABLED"))
        print(f"\nTotal agents: {stats['total_agents']}")
        print(f"Worker agents: {stats['worker_agents']}")
        print(f"Flash agents: {stats['flash_agents']}")
        print(f"Pro agents: {stats['pro_agents']}")
        print(f"Agents with planner: {stats['agents_with_planner']}")
        print(f"\nAuthentication: {self.auth_config.get_auth_type() if hasattr(self.auth_config, 'get_auth_type') else 'configured'}")

    async def run(self) -> None:
        """
        Glavni run metod

        Pokreće sustav u interaktivnom ili batch modu
        """
        try:
            # Inicijaliziraj agente (CLI pokreće scheduler)
            self.initialize_agents(start_scheduler=True)

            # Provjeri autentifikaciju
            if not self.verify_authentication():
                logger.warning("Authentication not configured. Some features may not work.")

            # Pokreni interaktivni mod
            await self.run_interactive()

        except Exception as e:
            logger.error(f"System error: {e}")
            raise
        finally:
            # Shutdown scheduler gracefully
            if self.scheduler and self.scheduler.scheduler.running:
                self.scheduler.scheduler.shutdown(wait=False)
                logger.info("Scheduler shut down")


async def main():
    """Main entry point"""
    system = WorkspaceADKSystem()
    await system.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nShutdown complete")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
