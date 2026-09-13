#!/usr/bin/env bash
set -euo pipefail

# Must match WorkingDirectory= in deploy/rpi/systemd/*.service, which pin the
# path and user of the live Pi. Change both together.
APP_DIR="${APP_DIR:-/home/raspberrypijarvis/google_claude}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_SYSTEMD_UNITS="${INSTALL_SYSTEMD_UNITS:-true}"
APT_PACKAGES=(
  git
  python3
  python3-venv
  python3-dev
  build-essential
  libffi-dev
  libasound2-dev
  alsa-utils
  portaudio19-dev
  ffmpeg
)

echo "Setting up Raspberry Pi deployment in ${APP_DIR}"
sudo apt-get update
sudo apt-get install -y "${APT_PACKAGES[@]}"

sudo mkdir -p "${APP_DIR}"
sudo chown -R "${USER}:${USER}" "${APP_DIR}"

if [[ ! -d "${APP_DIR}/.venv" ]]; then
  "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi

source "${APP_DIR}/.venv/bin/activate"
python -m pip install --upgrade pip wheel
# Pinned to the versions the live Pi runs.
pip install -r "${APP_DIR}/requirements.txt"

mkdir -p "${APP_DIR}/state/oauth"
mkdir -p "${APP_DIR}/logs"

if [[ ! -f "${APP_DIR}/deploy/rpi/.env.rpi" ]]; then
  cp "${APP_DIR}/deploy/rpi/.env.rpi.example" "${APP_DIR}/deploy/rpi/.env.rpi"
  echo "Created ${APP_DIR}/deploy/rpi/.env.rpi from example. Fill in secrets before starting services."
fi

if [[ "${INSTALL_SYSTEMD_UNITS}" == "true" ]]; then
  sudo cp "${APP_DIR}/deploy/rpi/systemd/"*.service /etc/systemd/system/
  sudo systemctl daemon-reload
  echo "Systemd units copied to /etc/systemd/system. Enable them after you fill in .env.rpi."
fi

echo "RPi setup complete."
