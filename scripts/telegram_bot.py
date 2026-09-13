"""
Google Workspace ADK Multi-Agent System
Telegram Bot Entry Point

This is the Telegram interface for the same agent system used in main.py.
All agents, tools, and functionality are shared between CLI and Telegram.

Usage:
    # Polling mode (local development):
    python telegram_bot.py

    # Webhook mode (set TELEGRAM_WEBHOOK_URL in .env):
    python telegram_bot.py --webhook

Environment Variables Required:
    TELEGRAM_BOT_TOKEN - Bot token from @BotFather
    TELEGRAM_CHAT_ID - Your Telegram chat ID for authorization

Optional:
    TELEGRAM_WEBHOOK_URL - Webhook URL for Cloud Run deployment
    TELEGRAM_AUTHORIZED_CHAT_IDS - Additional authorized chat IDs (comma-separated)
"""

import os
import sys
import asyncio
import logging
import argparse
from pathlib import Path

# This script lives in scripts/; put the repo root on sys.path so `config`,
# `interfaces`, etc. import whether launched as `python scripts/telegram_bot.py`
# or from systemd (where CWD is the repo root but sys.path[0] is scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Setup logging before imports
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()
from config.deployment_config import is_telegram_enabled
from utils.process_guard import hard_exit


def check_requirements():
    """Check if required dependencies are installed."""
    if not is_telegram_enabled():
        logger.error("Telegram interface is disabled by deployment profile")
        sys.exit(1)

    try:
        import telegram
        logger.info(f"python-telegram-bot version: {telegram.__version__}")
    except ImportError:
        logger.error("python-telegram-bot not installed!")
        logger.error("Run: pip install python-telegram-bot>=21.0")
        sys.exit(1)

    # Check required environment variables
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env file!")
        sys.exit(1)

    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set - bot will accept messages from anyone!")
        logger.warning("This is a security risk. Set TELEGRAM_CHAT_ID in .env")

    logger.info(f"Bot token: {token[:10]}...{token[-5:]}")
    logger.info(f"Authorized chat ID: {chat_id}")


async def run_polling():
    """Run the bot in polling mode."""
    from interfaces.telegram_interface import TelegramInterface

    logger.info("=" * 60)
    logger.info("Google Workspace ADK - Telegram Bot")
    logger.info("Mode: POLLING (local development)")
    logger.info("=" * 60)

    interface = TelegramInterface()

    try:
        await interface.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await interface.stop()


async def run_webhook():
    """Run the bot in webhook mode with a simple HTTP server."""
    from interfaces.telegram_interface import TelegramInterface

    logger.info("=" * 60)
    logger.info("Google Workspace ADK - Telegram Bot")
    logger.info("Mode: WEBHOOK")
    logger.info("=" * 60)

    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL")
    if not webhook_url:
        logger.error("TELEGRAM_WEBHOOK_URL not set for webhook mode!")
        sys.exit(1)

    # For production webhook, you would typically use FastAPI or Flask
    # This is a simple example using aiohttp
    try:
        from aiohttp import web
    except ImportError:
        logger.error("aiohttp not installed for webhook mode!")
        logger.error("Run: pip install aiohttp")
        sys.exit(1)

    interface = TelegramInterface()

    # Initialize the interface
    interface.initialize_system()

    # Build application
    from telegram.ext import ApplicationBuilder
    interface.application = ApplicationBuilder().token(interface.bot_token).build()
    interface._setup_handlers()
    await interface.application.initialize()
    await interface._setup_bot_commands()

    # Set webhook (secret token lets us reject forged POSTs)
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET is not set — webhook updates cannot be "
            "authenticated."
        )
    await interface.application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        secret_token=webhook_secret or None,
    )
    logger.info(f"Webhook set to: {webhook_url}")

    # Webhook handler
    async def handle_webhook(request):
        """Handle incoming webhook requests."""
        try:
            data = await request.json()
            secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            await interface.process_webhook_update(data, secret_header=secret_header)
            return web.Response(text="OK")
        except PermissionError:
            return web.Response(text="Forbidden", status=403)
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.Response(text="Error", status=500)

    # Health check endpoint
    async def health_check(request):
        """Health check for Cloud Run."""
        return web.Response(text="OK")

    # Create web app
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)

    # Get port from environment (Cloud Run sets PORT)
    port = int(os.getenv("PORT", 8080))

    # Run server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)

    logger.info(f"Starting webhook server on port {port}")
    await site.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        await interface.stop()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Google Workspace ADK - Telegram Bot"
    )
    parser.add_argument(
        "--webhook",
        action="store_true",
        help="Run in webhook mode (requires TELEGRAM_WEBHOOK_URL)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("telegram").setLevel(logging.DEBUG)

    # Check requirements
    check_requirements()

    # Run appropriate mode
    #
    # Every exit path below goes through hard_exit() on purpose. sys.exit() only
    # raises SystemExit and the interpreter then waits for non-daemon threads --
    # the Cloud Logging handler owns one and can block forever flushing its
    # backlog. That is how this service survived its own fatal error on
    # 2026-08-18 and sat there for two days with systemd reporting it healthy.
    try:
        if args.webhook:
            asyncio.run(run_webhook())
        else:
            asyncio.run(run_polling())
    except KeyboardInterrupt:
        logger.info("\nShutdown complete")
        hard_exit(0, "interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        hard_exit(1, f"fatal error: {e}")
    else:
        # Falling out of the run loop is not normal for a daemon: exit non-zero so
        # systemd restarts instead of leaving a silent, bot-less process behind.
        logger.error("Bot run loop returned unexpectedly")
        hard_exit(1, "run loop returned unexpectedly")


if __name__ == "__main__":
    main()
