"""Synthesize a Croatian sentence with a Cloud TTS Chirp3-HD voice and save a
WAV so it can be played on the Pi speaker. Usage:
    python scripts/cloud_tts_probe.py [voice_name] [text]
"""
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

import google.auth
from google.auth.transport.requests import Request

voice = sys.argv[1] if len(sys.argv) > 1 else "hr-HR-Chirp3-HD-Charon"
text = sys.argv[2] if len(sys.argv) > 2 else (
    "Dobar dan, ja sam Jarvis, vaš glasovni asistent. "
    "Drago mi je što mogu pomoći. Treba li još nešto?"
)
lang = "-".join(voice.split("-")[:2]) if "-" in voice else "hr-HR"

creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
creds.refresh(Request())

payload = {
    "input": {"text": text},
    "voice": {"languageCode": lang, "name": voice},
    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
}
req = urllib.request.Request(
    "https://texttospeech.googleapis.com/v1/text:synthesize",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": "Bearer %s" % creds.token,
        "Content-Type": "application/json; charset=utf-8",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    body = json.loads(resp.read().decode("utf-8"))

wav = base64.b64decode(body["audioContent"])
out = "/tmp/jarvis_tts_%s.wav" % voice.replace("-", "_")
with open(out, "wb") as f:
    f.write(wav)
print("voice=%s lang=%s bytes=%d" % (voice, lang, len(wav)))
print("saved=%s" % out)
