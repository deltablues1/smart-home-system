# Deployment

> 🇭🇷 [Hrvatska verzija](../hr/postavljanje.md)

How the Jarvis Pi is set up and kept running. The paths and user below are the
ones the live Pi uses and the systemd units pin; if yours differ, change the
units and `deploy/rpi/setup.sh` together (a unit test checks they agree).

---

## 1. Base system

Raspberry Pi OS (64-bit) on a Raspberry Pi 5, Python 3.11, SSH with a key, and a
static address set on the Pi itself.

```bash
sudo apt-get update && sudo apt-get full-upgrade -y
```

## 2. Code and dependencies

```bash
git clone <repository> /home/raspberrypijarvis/google_claude
cd /home/raspberrypijarvis/google_claude
./deploy/rpi/setup.sh
```

`setup.sh` installs the system packages (ALSA, PortAudio, ffmpeg, build tools),
creates `.venv`, installs the pinned `requirements.txt`, and copies the systemd
units into `/etc/systemd/system`.

## 3. Configuration and secrets

```bash
cp .env.example .env
nano .env
mkdir -p secrets && chmod 700 secrets
# copy the Google service-account JSON into secrets/, then:
chmod 600 .env secrets/*
```

[`.env.example`](../../.env.example) is grouped by subsystem and carries the
tuned values. Two rules: comments go on their own line (systemd's
`EnvironmentFile=` keeps an inline comment as part of the value), and a
duplicated key is resolved in favour of the **last** occurrence.

### Google accounts

- **Vertex AI, Firestore, Cloud TTS/STT** — the service account.
- **Gmail, Calendar, Drive, Docs, Sheets, Contacts, Tasks** — OAuth as the
  household account. Authorise once:

  ```bash
  .venv/bin/python tools/oauth_cli.py --auth     # local browser callback
  .venv/bin/python tools/oauth_cli.py --manual   # paste the redirect URL, works from a phone
  ```

  Keep the requested scopes to what the tools use. Requesting the broad
  `cloud-platform` scope on a user token triggered Google's periodic
  re-authentication and silently killed the token every morning.

- **Gemini Transcribe** — a Developer API key with AI Studio credit, which is
  billed separately from Google Cloud.

## 4. Audio

```bash
bash deploy/rpi/setup_audio.sh
arecord -l && aplay -l
```

Set `WAKEWORD_INPUT_DEVICE` to the capture device and keep
`WAKEWORD_OUTPUT_DEVICE=default` (see [hardware](hardware.md#jarvis-pi-audio)).

## 5. Services

```bash
sudo systemctl enable --now adk-web adk-wakeword adk-telegram adk-scheduler adk-wyoming
journalctl -u adk-web -f
```

| Unit | Watchdog | Notes |
|------|----------|-------|
| `adk-web` | — | runs `scripts/check_oauth_health.py` before start |
| `adk-wakeword` | — | needs the audio device |
| `adk-telegram` | 180 s | exits hard on a fatal error so systemd restarts it |
| `adk-scheduler` | 180 s | the **only** process that executes jobs |
| `adk-wyoming` | yes | port 10400 for Home Assistant |

Restarting needs `sudo`; the application user does not have it. As a fallback,
killing a service's main process as its own user works too: `Restart=always`
brings it back with a freshly read environment file.

## 6. Home Assistant side

1. Copy `deploy/home_assistant/custom_components/jarvis/` to
   `/config/custom_components/` and restart Home Assistant (a code change needs a
   full restart, not an entry reload).
2. Add the **Jarvis** integration (address and `API_TOKEN`) and the **Wyoming
   Protocol** integration (Jarvis Pi, port 10400).
3. Create an Assist pipeline *Jarvis* with those three components.
4. Copy `deploy/home_assistant/www/*.js` to `/config/www/`, register
   `jarvis-sky.js` under `frontend: extra_module_url`, restart.
5. Build the dashboard: `HA_URL=... HA_TOKEN=... python
   deploy/home_assistant/tools/build_dashboard.py`.

Details: [`JARVIS_ASSIST.md`](../../deploy/home_assistant/JARVIS_ASSIST.md).

## 7. Wall panel

`deploy/rpi/panel-kiosk.sh` is started from `~/.config/labwc/autostart`
(example in `deploy/rpi/labwc-autostart`). It needs no root and is undone by
deleting one line.

---

## Updating

```bash
# on the development machine
git push
# on the Pi
cd ~/google_claude && git pull --ff-only
sudo systemctl restart adk-web adk-wakeword adk-telegram adk-scheduler adk-wyoming
```

Code pulled without a restart does nothing. Before a deploy that changes stored
formats, snapshot runtime state:

```bash
.venv/bin/python scripts/backup_runtime_state.py
```

It saves the session store, `data/` and the current commit; restoring is
deliberately manual.

## Checking a deployment

```bash
.venv/bin/python scripts/rpi_smoke_check.py      # profile, env, audio, broker
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/api/status
```

Useful log lines: `Direct voice route ->` shows which lane took a spoken
request, `[TOOL]` and `[TOOL=]` show each tool call inside an agent and its
result, `[TOKENS]` shows what a turn cost.

## Security notes

- The API requires a bearer token on the `rpi-home` profile and binds to the
  LAN; nothing is exposed to the internet. Remote access goes through Home
  Assistant over Tailscale.
- Secrets live in `.env` and `secrets/`, both ignored by git and readable only
  by the service user.
- Telegram accepts only the chat ids in `TELEGRAM_AUTHORIZED_CHAT_IDS`.
