"""
Force OAuth Login

Forces OAuth authentication flow and opens browser.

Uses the project's OAuthManager (web client flow) so that the redirect URI
matches exactly what is registered in Google Cloud Console
(http://localhost:8080/oauth2callback). A tiny local HTTP server captures the
authorization code on that exact path and exchanges it for a token.

Usage:
    py scripts/force_oauth_login.py
"""

import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Fix encoding on Windows
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Load environment
load_dotenv()

# Allow http://localhost redirect during local login and tolerate Google
# returning scopes in a different order than requested.
os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')


# Holds the captured authorization code (or error) from the redirect.
_auth_result = {"code": None, "error": None}


def _make_handler(callback_path: str):
    class _OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            _auth_result["code"] = (params.get("code") or [None])[0]
            _auth_result["error"] = (params.get("error") or [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if _auth_result["code"]:
                msg = "Authentication successful! You can close this window."
            else:
                msg = f"Authentication failed: {_auth_result['error'] or 'no code returned'}"
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode("utf-8"))

        def log_message(self, *args):  # silence default request logging
            pass

    return _OAuthCallbackHandler


def _free_port_8080():
    """Best-effort kill of any stale process holding port 8080 (Windows only)."""
    if sys.platform != 'win32':
        return
    import subprocess
    try:
        subprocess.run(
            ['cmd', '/c',
             'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :8080 ^| findstr LISTENING\') '
             'do taskkill /PID %a /F'],
            capture_output=True, text=True
        )
    except Exception:
        pass


def main():
    print("=" * 70)
    print("Force OAuth Authentication")
    print("=" * 70)

    creds_file = os.path.join(project_root, 'oauth_client_credentials.json')
    if not os.path.exists(creds_file):
        print("\n❌ Error: oauth_client_credentials.json not found!")
        print(f"   Expected location: {creds_file}")
        print("\n💡 Please ensure OAuth credentials file exists")
        return

    print("\n✅ Found credentials file: oauth_client_credentials.json")

    from auth.oauth_manager import get_oauth_manager

    manager = get_oauth_manager()
    redirect_uri = manager.redirect_uri
    parsed_redirect = urlparse(redirect_uri)
    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 8080
    callback_path = parsed_redirect.path or "/oauth2callback"

    print(f"\n🔑 OAuth client: {(manager.client_id or '')[:32]}...")
    print(f"   Redirect URI : {redirect_uri}")
    print(f"   Scopes       : {len(manager.SCOPES)}")

    if port == 8080:
        _free_port_8080()

    auth_url = manager.get_authorization_url()

    handler = _make_handler(callback_path)
    try:
        server = HTTPServer((host if host != "localhost" else "127.0.0.1", port), handler)
    except OSError as e:
        print(f"\n❌ Could not bind local server on {host}:{port} -> {e}")
        print("   Make sure no other process is using that port.")
        return

    print("\n🌐 Opening browser for authentication...")
    print(f"   If it does not open, visit this URL manually:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    # Serve requests until the callback is received.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("   Waiting for Google redirect (complete the consent in your browser)...")
    try:
        while _auth_result["code"] is None and _auth_result["error"] is None:
            server_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\n❌ Cancelled by user.")
        server.shutdown()
        return
    finally:
        server.shutdown()

    if _auth_result["error"] or not _auth_result["code"]:
        print(f"\n❌ Authentication failed: {_auth_result['error'] or 'no authorization code returned'}")
        _print_troubleshooting()
        return

    print("\n🔄 Exchanging authorization code for token...")
    try:
        credentials = manager.exchange_code_for_token(_auth_result["code"])
    except Exception as e:
        print(f"\n❌ Token exchange error: {e}")
        import traceback
        traceback.print_exc()
        _print_troubleshooting()
        return

    if credentials and credentials.valid:
        print("\n✅ Authentication successful!")
        print(f"   Token saved to: {manager.token_storage_path}")

        scopes = list(credentials.scopes or manager.SCOPES)
        print("\n📋 Granted permissions:")
        for scope in scopes[:8]:
            print(f"   ✓ {scope.split('/')[-1]}")
        if len(scopes) > 8:
            print(f"   ... and {len(scopes) - 8} more")

        print("\n✅ You can now run the system:")
        print("   py main.py")
        print("   py run_web.py")
    else:
        print("\n❌ Authentication failed! Credentials are not valid.")
        _print_troubleshooting()

    print("\n" + "=" * 70)


def _print_troubleshooting():
    print("\n💡 Troubleshooting:")
    print("   1. Check oauth_client_credentials.json is valid JSON for the right project")
    print("   2. Verify GOOGLE_OAUTH_CLIENT_ID / SECRET in .env match the JSON")
    print("   3. In Google Cloud Console, the OAuth client must list the redirect URI:")
    print("      http://localhost:8080/oauth2callback")
    print("   4. Ensure the OAuth consent screen is configured and your account is a test user")
    print("   5. Make sure port 8080 is free")


if __name__ == "__main__":
    main()
