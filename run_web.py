"""
Web Dashboard Runner for Google Workspace ADK Multi-Agent System

Usage:
    python run_web.py                    # Start on localhost:8000
    python run_web.py --port 9000        # Custom port
    python run_web.py --host 0.0.0.0     # Bind to all interfaces (for Docker/Cloud Run)

Bind address and port also read WEB_HOST / WEB_PORT from the environment, so a
deployment can change them in .env without touching the systemd unit.
"""

import os
import sys
import logging
import argparse

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

# Set interface context so HITL logic knows not to use blocking terminal input
os.environ.setdefault('HITL_INTERFACE', 'web')


def main():
    parser = argparse.ArgumentParser(description="Google Workspace ADK Web Dashboard")
    # Defaults come from the environment so the bind address can be changed in
    # .env instead of the systemd unit -- editing a unit needs root, .env does not.
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"),
                        help="Host to bind to (default: WEB_HOST env, else 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8000")),
                        help="Port to bind to (default: WEB_PORT env, else 8000)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Google Workspace ADK - Web Dashboard")
    print(f"  Starting at http://{args.host}:{args.port}")
    print("=" * 60)
    print()

    # Import here to avoid circular imports
    from interfaces.web_interface import WebInterface
    from web.app import create_app

    # Create interface (system initialization happens in FastAPI lifespan
    # because APScheduler.start() needs a running event loop)
    interface = WebInterface()

    # Create FastAPI app with lifespan-based init
    app = create_app(interface)

    # Report this Pi's own health to Home Assistant. It rides along with the web
    # service rather than getting its own unit, which also makes the reading
    # honest: if the API is down, Home Assistant should say Jarvis is down.
    from services.host_metrics import HostMetricsPublisher
    metrics = HostMetricsPublisher()
    metrics.start()

    # Run with uvicorn (starts the event loop, triggers lifespan startup)
    import uvicorn
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"API Docs:  http://{args.host}:{args.port}/docs")
    print()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        metrics.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutdown complete")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
