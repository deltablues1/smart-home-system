"""
Refresh OAuth Token

Deletes expired token and forces re-authentication.

Usage:
    py scripts/refresh_oauth.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Fix encoding on Windows
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')


def find_token_file():
    """Find token.json file"""
    possible_locations = [
        Path(project_root) / "token.json",
        Path(project_root) / "auth" / "token.json",
        Path(project_root) / ".auth" / "token.json",
        Path.home() / ".google" / "token.json",
    ]

    for path in possible_locations:
        if path.exists():
            return path
    return None


def main():
    print("=" * 70)
    print("OAuth Token Refresh")
    print("=" * 70)

    # Find token file
    token_path = find_token_file()

    if token_path:
        print(f"\n📁 Found token file: {token_path}")
        print(f"   Deleting expired token...")

        try:
            token_path.unlink()
            print(f"   ✅ Token file deleted")
        except Exception as e:
            print(f"   ❌ Failed to delete: {e}")
            return
    else:
        print("\n⚠️  Token file not found (may already be deleted)")

    print("\n" + "=" * 70)
    print("Re-Authentication Required")
    print("=" * 70)

    print("\n📋 Next steps:")
    print("   1. Run main.py or any script that uses Google APIs")
    print("   2. Browser will open for OAuth consent")
    print("   3. Sign in with your Google account")
    print("   4. Grant permissions")
    print("   5. New token will be created automatically")

    print("\n💡 Quick test:")
    print("   py main.py")
    print("   (Browser will open for authentication)")

    print("\n" + "=" * 70)
    print("✅ Ready for re-authentication!")
    print("=" * 70)


if __name__ == "__main__":
    main()
