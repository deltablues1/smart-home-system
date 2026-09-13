"""Validate Cloud Speech-to-Text v2 (Chirp) recognition on a WAV file.
Usage: python scripts/cloud_stt_probe.py <wav_path> [model] [location] [lang]
Tries the given model/location; prints transcript or the API error."""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

import google.auth
from google.auth.transport.requests import Request

wav_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/jarvis_tts_hr_HR_Chirp3_HD_Charon.wav"
model = sys.argv[2] if len(sys.argv) > 2 else "chirp_2"
location = sys.argv[3] if len(sys.argv) > 3 else "us-central1"
lang = sys.argv[4] if len(sys.argv) > 4 else "hr-HR"

creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
creds.refresh(Request())

audio = Path(wav_path).read_bytes()
url = "https://%s-speech.googleapis.com/v2/projects/%s/locations/%s/recognizers/_:recognize" % (
    location, project, location,
)
payload = {
    "config": {
        "model": model,
        "languageCodes": [lang],
        "features": {"enableAutomaticPunctuation": True},
        "autoDecodingConfig": {},
    },
    "content": base64.b64encode(audio).decode("ascii"),
}
req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Authorization": "Bearer %s" % creds.token, "Content-Type": "application/json"},
    method="POST",
)
print("model=%s location=%s lang=%s file=%s (%d bytes)" % (model, location, lang, wav_path, len(audio)))
try:
    with urllib.request.urlopen(req, timeout=40) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    parts = []
    for r in body.get("results", []):
        alts = r.get("alternatives") or []
        if alts and alts[0].get("transcript"):
            parts.append(alts[0]["transcript"])
    print("HTTP 200")
    print("TRANSCRIPT:", " ".join(p.strip() for p in parts).strip() or "(empty)")
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode("utf-8")[:500])
