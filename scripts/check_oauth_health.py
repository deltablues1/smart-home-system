#!/usr/bin/env python3
"""
OAuth Health Check
==================
Verifies that the OAuth token is valid and can be refreshed.
Designed for:
  - RPi systemd ExecStartPre (fail-fast if token is broken)
  - Cron job for daily monitoring

Exit codes:
  0 = Token valid (or refreshed successfully)
  1 = Token missing or broken (needs manual re-auth)

Usage:
  python scripts/check_oauth_health.py
  python scripts/check_oauth_health.py --verbose
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


def check_token_health(verbose: bool = False) -> bool:
    """Check if OAuth token exists, is valid, and can refresh."""
    logger = logging.getLogger("oauth_health")

    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")

    deployment_profile = os.getenv("DEPLOYMENT_PROFILE", "full")
    require_healthcheck = os.getenv(
        "REQUIRE_OAUTH_HEALTHCHECK",
        "false" if deployment_profile == "rpi-home" else "true",
    ).lower() in ("1", "true", "yes", "on")

    if not require_healthcheck:
        logger.warning(
            "OAuth health check skipped (REQUIRE_OAUTH_HEALTHCHECK=false)"
        )
        return True

    # Determine token path
    token_path = (
        os.getenv('OAUTH_TOKEN_STORAGE_PATH')
        or os.path.join(Path.home(), '.google_workspace_adk', 'tokens.json')
    )

    # Step 1: Check token file exists
    if not os.path.exists(token_path):
        logger.error(f"FAIL: Token file not found at {token_path}")
        logger.error("Run 'python scripts/force_oauth_login.py' on a machine with a browser")
        return False

    logger.info(f"Token file: {token_path}")

    # Step 2: Load and validate token structure
    try:
        with open(token_path, 'r') as f:
            token_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"FAIL: Cannot read token file: {e}")
        return False

    if 'refresh_token' not in token_data:
        logger.error("FAIL: Token has no refresh_token - needs full re-auth")
        return False

    logger.info("Token has refresh_token")

    # Step 3: Check expiry
    expiry_str = token_data.get('expiry')
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
            now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.now()
            if expiry > now:
                remaining = expiry - now
                logger.info(f"Access token valid for {remaining}")
            else:
                logger.info("Access token expired - will refresh on next use")
        except ValueError:
            logger.info("Cannot parse expiry, will refresh on next use")

    # Step 4: Try to refresh
    try:
        from auth.oauth_manager import OAuthManager
        manager = OAuthManager()
        creds = manager.get_credentials()

        if creds and creds.valid:
            logger.info("OK: Token is valid")
            return True
        elif creds and creds.expired and creds.refresh_token:
            logger.info("Token expired, attempting refresh...")
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            manager._credentials = creds
            manager._save_token()
            logger.info("OK: Token refreshed successfully")
            return True
        else:
            logger.error("FAIL: Token invalid and cannot be refreshed")
            return False

    except Exception as e:
        logger.error(f"FAIL: Token refresh failed: {e}")
        logger.error("Run 'python scripts/force_oauth_login.py' to re-authenticate")
        return False


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    ok = check_token_health(verbose)
    sys.exit(0 if ok else 1)
