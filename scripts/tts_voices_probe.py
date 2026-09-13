"""Probe which Croatian (hr-HR) voices Google Cloud Text-to-Speech offers on
this project, and whether the TTS / Speech v2 APIs are enabled. Read-only."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

import google.auth
from google.auth.transport.requests import Request

creds, project = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
creds.refresh(Request())
token = creds.token
print("project=", project)


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:400]


# 1) Cloud Text-to-Speech: list hr-HR voices
status, body = get("https://texttospeech.googleapis.com/v1/voices?languageCode=hr-HR")
print("\n=== TTS voices?languageCode=hr-HR -> HTTP", status, "===")
if isinstance(body, dict):
    voices = body.get("voices", [])
    print("count=", len(voices))
    for v in voices:
        print("  ", v.get("name"), v.get("ssmlGender"), v.get("naturalSampleRateHertz"))
else:
    print(body)

# 2) Is Speech-to-Text v2 API reachable? (list recognizers in us-central1 location)
status2, body2 = get(
    "https://speech.googleapis.com/v2/projects/%s/locations/global/recognizers" % project
)
print("\n=== Speech-to-Text v2 recognizers (global) -> HTTP", status2, "===")
print(body2 if isinstance(body2, str) else json.dumps(body2)[:300])
