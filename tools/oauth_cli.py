"""
OAuth CLI Flow

Command-line interface za OAuth 2.0 authorization flow
Omogućava korisnicima da se autenticiraju i dobiju OAuth token
"""

import os
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading
import logging
from typing import Optional
from pathlib import Path

# Add project root to path
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from auth.oauth_manager import OAuthManager, get_oauth_manager

logger = logging.getLogger(__name__)


# ============================================================================
# OAUTH CALLBACK SERVER
# ============================================================================

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP request handler za OAuth callback"""

    authorization_code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):
        """Handle GET request from OAuth callback"""
        # Parse query parameters
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)

        # Check for authorization code
        if 'code' in query_params:
            OAuthCallbackHandler.authorization_code = query_params['code'][0]
            self.send_success_response()
        elif 'error' in query_params:
            OAuthCallbackHandler.error = query_params['error'][0]
            self.send_error_response()
        else:
            self.send_error_response("No code or error in callback")

    def send_success_response(self):
        """Send success HTML response"""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Authorization Successful</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background-color: #f0f0f0;
                }
                .container {
                    background-color: white;
                    padding: 40px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    max-width: 500px;
                    margin: 0 auto;
                }
                h1 {
                    color: #4CAF50;
                }
                .checkmark {
                    font-size: 100px;
                    color: #4CAF50;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="checkmark">✓</div>
                <h1>Authorization Successful!</h1>
                <p>You have successfully authenticated with Google.</p>
                <p>You can now close this window and return to the terminal.</p>
            </div>
        </body>
        </html>
        """
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def send_error_response(self, error_message: str = "Authorization failed"):
        """Send error HTML response"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Authorization Failed</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background-color: #f0f0f0;
                }}
                .container {{
                    background-color: white;
                    padding: 40px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    max-width: 500px;
                    margin: 0 auto;
                }}
                h1 {{
                    color: #f44336;
                }}
                .error-icon {{
                    font-size: 100px;
                    color: #f44336;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error-icon">✗</div>
                <h1>Authorization Failed</h1>
                <p>{error_message}</p>
                <p>Please try again or check your credentials.</p>
            </div>
        </body>
        </html>
        """
        self.send_response(400)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        """Suppress server logging"""
        pass


def run_callback_server(port: int = 8080) -> Optional[str]:
    """
    Run local HTTP server to handle OAuth callback

    Args:
        port: Port to listen on (default: 8080)

    Returns:
        Authorization code or None if error

    """
    # Reset class variables
    OAuthCallbackHandler.authorization_code = None
    OAuthCallbackHandler.error = None

    # Create server
    server = HTTPServer(('localhost', port), OAuthCallbackHandler)

    print(f"Starting callback server on http://localhost:{port}")
    print("Waiting for authorization callback...")

    # Handle single request (the OAuth callback)
    server.handle_request()

    # Check result
    if OAuthCallbackHandler.authorization_code:
        print("✓ Authorization code received")
        return OAuthCallbackHandler.authorization_code
    elif OAuthCallbackHandler.error:
        print(f"✗ Authorization error: {OAuthCallbackHandler.error}")
        return None
    else:
        print("✗ No authorization code received")
        return None


# ============================================================================
# OAUTH CLI FUNCTIONS
# ============================================================================

def start_oauth_flow(oauth_manager: Optional[OAuthManager] = None) -> bool:
    """
    Start OAuth 2.0 authorization flow

    Args:
        oauth_manager: OAuthManager instance (optional, creates new if not provided)

    Returns:
        True if authorization successful, False otherwise
    """
    if oauth_manager is None:
        oauth_manager = get_oauth_manager()

    print("\n" + "="*60)
    print("Google Workspace ADK - OAuth 2.0 Authorization")
    print("="*60 + "\n")

    # Step 1: Generate authorization URL
    print("Step 1: Generating authorization URL...")
    try:
        auth_url = oauth_manager.get_authorization_url()
        print("✓ Authorization URL generated\n")
    except Exception as e:
        print(f"✗ Failed to generate authorization URL: {e}")
        return False

    # Step 2: Open browser
    print("Step 2: Opening browser for authorization...")
    print(f"\nIf the browser doesn't open automatically, visit this URL:\n{auth_url}\n")

    try:
        webbrowser.open(auth_url)
        print("✓ Browser opened\n")
    except Exception as e:
        print(f"⚠ Failed to open browser automatically: {e}")
        print("Please manually open the URL above.\n")

    # Step 3: Wait for callback
    print("Step 3: Waiting for authorization callback...")
    print("Please complete the authorization in your browser.\n")

    # Extract port from redirect URI
    redirect_uri = oauth_manager.redirect_uri
    parsed_uri = urlparse(redirect_uri)
    port = parsed_uri.port or 8080

    authorization_code = run_callback_server(port=port)

    if not authorization_code:
        print("\n✗ Authorization failed. No authorization code received.")
        return False

    # Step 4: Exchange code for token
    print("\nStep 4: Exchanging authorization code for access token...")
    try:
        credentials = oauth_manager.exchange_code_for_token(authorization_code)
        print("✓ Access token obtained and saved")
    except Exception as e:
        print(f"✗ Failed to exchange authorization code: {e}")
        return False

    # Success
    print("\n" + "="*60)
    print("✓ Authorization Successful!")
    print("="*60)
    print("\nYou are now authenticated and ready to use Google Workspace APIs.")
    print(f"Token saved to: {oauth_manager.token_storage_path}\n")

    return True


def _extract_auth_code(raw: str) -> Optional[str]:
    """Pull the OAuth `code` out of a pasted redirect URL, query string, or raw code."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "code=" in raw:
        query = urlparse(raw).query or raw.split("?", 1)[-1]
        params = parse_qs(query)
        if params.get("code"):
            return params["code"][0]
    return raw  # assume the user pasted just the code


def start_oauth_flow_manual(oauth_manager: Optional[OAuthManager] = None) -> bool:
    """OAuth flow for headless hosts: no callback server, no browser on this box.

    Prints the auth URL to open on ANY browser (laptop/phone), then accepts the
    redirect URL/code pasted back. Avoids the localhost:8080 callback entirely.
    """
    if oauth_manager is None:
        oauth_manager = get_oauth_manager()

    print("\n" + "=" * 60)
    print("Google Workspace ADK - OAuth 2.0 (manual / headless)")
    print("=" * 60 + "\n")

    try:
        auth_url = oauth_manager.get_authorization_url()
    except Exception as e:
        print(f"✗ Failed to generate authorization URL: {e}")
        return False

    print("1) Open this URL in a real browser WITH JavaScript (laptop/phone):\n")
    print(auth_url + "\n")
    print("2) Sign in with the Google account Jarvis acts as, and approve access.")
    print("3) Your browser will then try to load a URL like:")
    print("     http://localhost:8080/oauth2callback?code=XXXX&scope=...")
    print("   It will show a 'can't reach the site' error — THAT IS FINE.")
    print("   Copy the FULL URL from the address bar.\n")

    raw = input("Paste the full redirect URL (or just the code): ").strip()
    code = _extract_auth_code(raw)
    if not code:
        print("✗ No authorization code found in what you pasted.")
        return False

    print("\nExchanging authorization code for access token...")
    try:
        oauth_manager.exchange_code_for_token(code)
    except Exception as e:
        print(f"✗ Failed to exchange authorization code: {e}")
        return False

    print("\n" + "=" * 60)
    print("✓ Authorization Successful!")
    print("=" * 60)
    print(f"Token saved to: {oauth_manager.token_storage_path}\n")
    return True


def check_oauth_status(oauth_manager: Optional[OAuthManager] = None) -> bool:
    """
    Check OAuth authentication status

    Args:
        oauth_manager: OAuthManager instance (optional)

    Returns:
        True if authenticated, False otherwise
    """
    if oauth_manager is None:
        oauth_manager = get_oauth_manager()

    print("\n" + "="*60)
    print("OAuth Authentication Status")
    print("="*60 + "\n")

    # Check credentials
    credentials = oauth_manager.get_credentials()

    if credentials and credentials.valid:
        print("✓ Authenticated")
        print(f"Token storage: {oauth_manager.token_storage_path}")
        print(f"Token valid: Yes")

        # Show scopes
        if hasattr(credentials, 'scopes'):
            print(f"\nGranted scopes:")
            for scope in credentials.scopes:
                print(f"  - {scope}")

        print()
        return True
    else:
        print("✗ Not authenticated")
        print("\nTo authenticate, run:")
        print("  python tools/oauth_cli.py --auth")
        print()
        return False


def revoke_oauth_token(oauth_manager: Optional[OAuthManager] = None) -> bool:
    """
    Revoke OAuth token

    Args:
        oauth_manager: OAuthManager instance (optional)

    Returns:
        True if revocation successful, False otherwise
    """
    if oauth_manager is None:
        oauth_manager = get_oauth_manager()

    print("\n" + "="*60)
    print("Revoke OAuth Token")
    print("="*60 + "\n")

    # Confirm
    confirm = input("Are you sure you want to revoke your OAuth token? (yes/no): ")
    if confirm.lower() not in ['yes', 'y']:
        print("Revocation cancelled.")
        return False

    # Revoke
    print("\nRevoking token...")
    try:
        success = oauth_manager.revoke_token()
        if success:
            print("✓ Token revoked successfully")
            print("\nYou will need to re-authenticate to use Google Workspace APIs.")
            return True
        else:
            print("✗ Failed to revoke token")
            return False
    except Exception as e:
        print(f"✗ Error revoking token: {e}")
        return False


# ============================================================================
# CLI MAIN
# ============================================================================

def main():
    """Main CLI entry point"""
    import argparse

    # This file prints check marks and crosses, and the Windows console
    # defaults to cp1250, which cannot encode them — so `--status` died on
    # the first glyph before saying anything useful. reconfigure() adjusts
    # the existing stream rather than replacing it, which is the difference
    # between this being safe and it breaking anything that wrapped stdout.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Google Workspace ADK - OAuth 2.0 Authorization CLI"
    )
    parser.add_argument(
        '--auth',
        action='store_true',
        help='Start OAuth authorization flow (local callback server on :8080)'
    )
    parser.add_argument(
        '--manual',
        action='store_true',
        help='Headless OAuth: print URL, paste redirect code back (no callback server)'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Check OAuth authentication status'
    )
    parser.add_argument(
        '--revoke',
        action='store_true',
        help='Revoke OAuth token'
    )

    args = parser.parse_args()

    # Load environment
    from dotenv import load_dotenv
    load_dotenv()

    # Check environment variables
    if not os.getenv('GOOGLE_OAUTH_CLIENT_ID') or not os.getenv('GOOGLE_OAUTH_CLIENT_SECRET'):
        print("✗ Error: OAuth credentials not configured")
        print("\nPlease set these environment variables in your .env file:")
        print("  GOOGLE_OAUTH_CLIENT_ID=your-client-id")
        print("  GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret")
        print("  GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8080/oauth2callback")
        sys.exit(1)

    # Execute command
    if args.manual:
        success = start_oauth_flow_manual()
        sys.exit(0 if success else 1)
    elif args.auth:
        success = start_oauth_flow()
        sys.exit(0 if success else 1)
    elif args.status:
        is_authenticated = check_oauth_status()
        sys.exit(0 if is_authenticated else 1)
    elif args.revoke:
        success = revoke_oauth_token()
        sys.exit(0 if success else 1)
    else:
        # Default: show status
        check_oauth_status()
        print("For more options, run: python tools/oauth_cli.py --help")


if __name__ == "__main__":
    main()
