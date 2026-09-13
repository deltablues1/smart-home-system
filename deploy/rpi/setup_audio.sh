#!/usr/bin/env bash
#
# Configure the WM8960 audio HAT mixer for voice use on the Raspberry Pi.
#
# The wm8960 powers up with its output mixers routed off and capture gain at a
# level that makes speech either inaudible (playback) or untranscribable
# (capture). These settings were tuned empirically for the wake-word voice loop:
#   - Playback: route DAC -> output mixers, unmute Speaker/Headphone at full.
#   - Capture:  moderate gain (50%, no input boost) — full boost CLIPS and the
#               STT model returns garbage/empty; too low and it mishears.
#
# Also note (handled in .env, not here):
#   WAKEWORD_OUTPUT_DEVICE=default   # raw hw:2,0 rejects 24 kHz TTS; "default"
#                                    # (ALSA plug) resamples automatically.
#   WAKEWORD_INPUT_DEVICE=0          # raw wm8960 capture, 16 kHz mono, works.
#
# Run once after flashing/driver install. Settings are persisted via alsactl.

set -euo pipefail

CARD="${WM8960_CARD:-2}"   # `aplay -l` -> wm8960-soundcard card index

echo "Configuring wm8960 mixer on card ${CARD}..."

# --- Playback / output ---
amixer -c "$CARD" sset 'Playback' 100%               >/dev/null
amixer -c "$CARD" sset 'Speaker' 100% unmute         >/dev/null
amixer -c "$CARD" sset 'Headphone' 100% unmute       >/dev/null
amixer -c "$CARD" sset 'Left Output Mixer PCM'  on   >/dev/null
amixer -c "$CARD" sset 'Right Output Mixer PCM' on   >/dev/null
amixer -c "$CARD" sset 'Mono Output Mixer Left'  on  >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Mono Output Mixer Right' on  >/dev/null 2>&1 || true

# --- Capture / input (moderate gain — do NOT max, it clips) ---
amixer -c "$CARD" sset 'Capture' 50%                          >/dev/null
amixer -c "$CARD" sset 'Left Input Mixer Boost'  on           >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Right Input Mixer Boost' on           >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Left Boost Mixer LINPUT1'  on         >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Right Boost Mixer RINPUT1' on         >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Left Input Boost Mixer LINPUT1'  0    >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'Right Input Boost Mixer RINPUT1' 0    >/dev/null 2>&1 || true
amixer -c "$CARD" sset 'ADC PCM' 100%                         >/dev/null 2>&1 || true

# Persist so the settings survive reboot.
( sudo alsactl store 2>/dev/null || alsactl store 2>/dev/null ) && echo "Mixer stored." || echo "Could not persist (run 'sudo alsactl store')."

echo "Done. Test: speaker-test -c1 -twav -D default ; arecord -d3 -fS16_LE -r16000 -c1 t.wav && aplay -Ddefault t.wav"
