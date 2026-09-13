#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check Available Vertex AI Models

This script checks which Gemini models are actually available in your Vertex AI project.
Helps diagnose 404 errors by testing different model versions and configurations.
"""

import sys
import os
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment
from dotenv import load_dotenv
load_dotenv()


def print_section(title):
    """Print section header"""
    print("\n" + "="*80)
    print(f"  {title}")
    print("="*80)


def test_environment():
    """Test environment configuration"""
    print_section("1. ENVIRONMENT CONFIGURATION")

    project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
    location = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')
    api_key = os.getenv('GOOGLE_API_KEY')
    credentials = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')

    print(f"\n  Project ID:          {project_id or '❌ NOT SET'}")
    print(f"  Location:            {location}")
    print(f"  API Key:             {'✅ SET (' + str(len(api_key)) + ' chars)' if api_key else '❌ NOT SET'}")
    print(f"  Service Account:     {credentials or '❌ NOT SET'}")

    if not project_id:
        print("\n  ⚠️  WARNING: GOOGLE_CLOUD_PROJECT not set!")
        print("     Set it in .env file: GOOGLE_CLOUD_PROJECT=your-project-id")
        return False

    return True


def test_auth():
    """Test authentication"""
    print_section("2. AUTHENTICATION TEST")

    try:
        import google.auth

        credentials, project = google.auth.default()

        print(f"\n  ✅ Authentication successful!")
        print(f"     Credentials type: {type(credentials).__name__}")
        print(f"     Project (from auth): {project}")

        # Check if credentials are valid
        if hasattr(credentials, 'valid'):
            print(f"     Credentials valid: {credentials.valid}")

        if hasattr(credentials, 'expired'):
            print(f"     Credentials expired: {credentials.expired}")

        return True

    except Exception as e:
        print(f"\n  ❌ Authentication failed!")
        print(f"     Error: {e}")
        print("\n  💡 Fix:")
        print("     Run: gcloud auth application-default login")
        return False


def test_vertex_ai_import():
    """Test if Vertex AI SDK is available"""
    print_section("3. VERTEX AI SDK TEST")

    try:
        import vertexai
        print(f"\n  ✅ vertexai package installed")
        print(f"     Version: {vertexai.__version__ if hasattr(vertexai, '__version__') else 'unknown'}")
        return True
    except ImportError as e:
        print(f"\n  ❌ vertexai package not installed!")
        print(f"     Error: {e}")
        print("\n  💡 Fix:")
        print("     Run: pip install google-cloud-aiplatform")
        return False


def test_vertex_ai_init():
    """Test Vertex AI initialization"""
    print_section("4. VERTEX AI INITIALIZATION")

    try:
        import vertexai

        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        location = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')

        print(f"\n  Initializing Vertex AI...")
        print(f"     Project: {project_id}")
        print(f"     Location: {location}")

        vertexai.init(project=project_id, location=location)

        print(f"\n  ✅ Vertex AI initialized successfully!")
        return True

    except Exception as e:
        print(f"\n  ❌ Vertex AI initialization failed!")
        print(f"     Error: {e}")
        return False


def test_genai_client():
    """Test google.genai Client"""
    print_section("5. GOOGLE GENAI CLIENT TEST")

    try:
        from google import genai

        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        location = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')

        print(f"\n  Creating genai.Client...")
        print(f"     Mode: Vertex AI")
        print(f"     Project: {project_id}")
        print(f"     Location: {location}")

        client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location
        )

        print(f"\n  ✅ genai.Client created successfully!")
        return client

    except Exception as e:
        print(f"\n  ❌ genai.Client creation failed!")
        print(f"     Error: {e}")
        return None


def test_specific_model(client, model_name):
    """Test a specific model"""
    try:
        print(f"\n  Testing: {model_name}")

        response = client.models.generate_content(
            model=model_name,
            contents="Hello! Say 'Test successful' if you can read this."
        )

        result_text = response.text if hasattr(response, 'text') else str(response)[:100]

        print(f"    ✅ SUCCESS! Model works!")
        print(f"       Response: {result_text[:80]}...")
        return True

    except Exception as e:
        error_str = str(e)
        if "404" in error_str:
            print(f"    ❌ 404 NOT FOUND - Model doesn't exist or no access")
        elif "403" in error_str:
            print(f"    ❌ 403 FORBIDDEN - No permission (IAM)")
        elif "401" in error_str:
            print(f"    ❌ 401 UNAUTHORIZED - Auth problem")
        else:
            print(f"    ❌ ERROR: {error_str[:100]}")

        return False


def test_all_model_versions(client):
    """Test all known Gemini model versions"""
    print_section("6. TESTING GEMINI MODELS")

    # Model versions to test (most common)
    models_to_test = [
        # Gemini 1.5 Pro versions
        "gemini-1.5-pro",           # Alias (may not work)
        "gemini-1.5-pro-002",       # Latest stable
        "gemini-1.5-pro-001",       # Older stable

        # Gemini 1.5 Flash versions
        "gemini-1.5-flash",         # Alias (may not work)
        "gemini-1.5-flash-002",     # Latest stable
        "gemini-1.5-flash-001",     # Older stable

        # Gemini 2.0 (if available)
        "gemini-2.0-flash-exp",     # Experimental

        # Gemini 1.0 (legacy)
        "gemini-1.0-pro",           # Old version
    ]

    print(f"\n  Testing {len(models_to_test)} model versions...\n")

    working_models = []
    failed_models = []

    for model_name in models_to_test:
        success = test_specific_model(client, model_name)

        if success:
            working_models.append(model_name)
        else:
            failed_models.append(model_name)

    # Summary
    print("\n" + "-"*80)
    print(f"\n  📊 SUMMARY:")
    print(f"     ✅ Working models: {len(working_models)}")
    print(f"     ❌ Failed models:  {len(failed_models)}")

    if working_models:
        print(f"\n  ✅ AVAILABLE MODELS:")
        for model in working_models:
            print(f"     • {model}")
    else:
        print(f"\n  ❌ NO WORKING MODELS FOUND!")

    return working_models


def provide_recommendations(working_models):
    """Provide recommendations based on test results"""
    print_section("7. RECOMMENDATIONS")

    if working_models:
        print("\n  ✅ Great! You have working models.")
        print("\n  💡 RECOMMENDED ACTION:")
        print(f"     Update your agent_registry.py to use: {working_models[0]}")
        print(f"\n     For Mailer agent (needs Pro):")
        if any('pro' in m for m in working_models):
            pro_model = next(m for m in working_models if 'pro' in m)
            print(f"       model: \"{pro_model}\"")
        else:
            print(f"       ⚠️  No Pro model available, use Flash instead")

        print(f"\n     For other agents (can use Flash):")
        if any('flash' in m for m in working_models):
            flash_model = next(m for m in working_models if 'flash' in m)
            print(f"       model: \"{flash_model}\"")

    else:
        print("\n  ❌ No models are working. This means:")
        print("\n  Possible causes:")
        print("     1. Vertex AI API not enabled in project")
        print("        → https://console.cloud.google.com/apis/library/aiplatform.googleapis.com")
        print("\n     2. No 'Vertex AI User' IAM role")
        print("        → https://console.cloud.google.com/iam-admin/iam")
        print("        → Add role: Vertex AI User")
        print("\n     3. Billing not enabled")
        print("        → https://console.cloud.google.com/billing")
        print("\n     4. Wrong region (models not available in us-central1)")
        print("        → Try: GOOGLE_CLOUD_LOCATION=us-east4 in .env")
        print("\n  💡 IMMEDIATE FIX:")
        print("     Use Google AI API instead of Vertex AI:")
        print("     1. Comment out GOOGLE_CLOUD_PROJECT in .env")
        print("     2. Keep only GOOGLE_API_KEY")
        print("     3. BaseAgent will fallback to API key mode")


def test_alternative_regions():
    """Test if models are available in different regions"""
    print_section("8. TESTING ALTERNATIVE REGIONS")

    try:
        from google import genai

        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')

        regions_to_test = [
            'us-central1',
            'us-east4',
            'us-west1',
            'europe-west1',
            'asia-southeast1'
        ]

        print(f"\n  Testing regions for model availability...")

        working_regions = []

        for region in regions_to_test:
            try:
                print(f"\n  Testing region: {region}")

                client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=region
                )

                # Try simple model
                response = client.models.generate_content(
                    model="gemini-1.5-flash-002",
                    contents="Test"
                )

                print(f"    ✅ {region} - WORKS!")
                working_regions.append(region)

            except Exception as e:
                if "404" in str(e):
                    print(f"    ❌ {region} - Model not available")
                else:
                    print(f"    ⚠️  {region} - Error: {str(e)[:50]}")

        if working_regions:
            print(f"\n  ✅ Working regions: {', '.join(working_regions)}")
            print(f"\n  💡 Set in .env:")
            print(f"     GOOGLE_CLOUD_LOCATION={working_regions[0]}")
        else:
            print(f"\n  ❌ No regions working - check IAM permissions")

        return working_regions

    except Exception as e:
        print(f"\n  ❌ Region testing failed: {e}")
        return []


def main():
    """Main test function"""
    print("\n" + "="*80)
    print("  🔍 VERTEX AI MODEL AVAILABILITY CHECKER")
    print("="*80)
    print("\n  This script will check:")
    print("  • Environment configuration")
    print("  • Authentication setup")
    print("  • Available Gemini models")
    print("  • Alternative regions")

    # Step 1: Environment
    if not test_environment():
        print("\n❌ Environment not configured properly. Fix .env file first.")
        return

    # Step 2: Authentication
    if not test_auth():
        print("\n❌ Authentication failed. Run: gcloud auth application-default login")
        return

    # Step 3: Vertex AI SDK
    if not test_vertex_ai_import():
        print("\n❌ Vertex AI SDK not installed. Run: pip install google-cloud-aiplatform")
        return

    # Step 4: Initialize Vertex AI
    if not test_vertex_ai_init():
        print("\n❌ Vertex AI initialization failed.")
        return

    # Step 5: Create genai client
    client = test_genai_client()
    if not client:
        print("\n❌ Could not create genai.Client")
        return

    # Step 6: Test models
    working_models = test_all_model_versions(client)

    # Step 7: Recommendations
    provide_recommendations(working_models)

    # Step 8: Alternative regions (only if no models work)
    if not working_models:
        test_alternative_regions()

    # Final summary
    print("\n" + "="*80)
    print("  📊 FINAL STATUS")
    print("="*80)

    if working_models:
        print(f"\n  ✅ SUCCESS! Found {len(working_models)} working model(s)")
        print(f"  🚀 You can proceed with testing")
    else:
        print(f"\n  ❌ NO WORKING MODELS")
        print(f"  ⚠️  Fix IAM permissions or use Google AI API instead")

    print("\n" + "="*80)


if __name__ == "__main__":
    main()
