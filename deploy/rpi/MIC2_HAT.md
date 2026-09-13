# Mic 2 HAT Preparation

This assumes you mean the `Seeed ReSpeaker 2-Mics Pi HAT`.

## Important note

Seeed's wiki page for this board explicitly documents Raspberry Pi support up to Raspberry Pi 4 in the listed compatibility section.

That means Raspberry Pi 5 should be treated as:
- likely workable
- but not clearly documented by Seeed in the published compatibility list I checked

## Recommended approach on Pi 5

1. Finish the base app deployment first.
2. Install the audio HAT driver and overlay.
3. Verify ALSA capture/playback.
4. Only then enable `adk-wakeword.service`.

## Driver / overlay flow

Seeed's Raspberry Pi guide documents this flow for the 2-Mics Pi HAT:

```bash
git clone https://github.com/Seeed-Studio/seeed-linux-dtoverlays.git
cd seeed-linux-dtoverlays
make overlays/rpi/respeaker-2mic-v1_0-overlay.dtbo
sudo cp overlays/rpi/respeaker-2mic-v1_0-overlay.dtbo /boot/firmware/overlays/respeaker-2mic-v1_0.dtbo
echo "dtoverlay=respeaker-2mic-v1_0" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

After reboot:

```bash
aplay -l
arecord -l
```

The documented expected card name is similar to:

```text
seeed-2mic-voicecard
```

## Quick ALSA test

Replace `3,0` with the actual card/device numbers you see on your Pi:

```bash
arecord -D "plughw:3,0" -f S16_LE -r 16000 -d 5 -t wav test.wav
aplay -D "plughw:3,0" test.wav
alsamixer
```

In `alsamixer`, use `F6` to select the ReSpeaker card first.

## How this maps to our app

Our wakeword runner uses `sounddevice`, not raw ALSA commands.

So after the driver works, check the Python-visible devices:

```bash
~/google_claude/.venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"
```

Then set in `~/google_claude/.env` only if needed:

```env
WAKEWORD_INPUT_DEVICE=
WAKEWORD_OUTPUT_DEVICE=
```

If the ReSpeaker appears and works as the default device, you can leave both empty.

## Practical recommendation

For first boot on Pi 5:
- do not enable `adk-wakeword.service` immediately
- first verify the HAT driver and recording manually
- after that enable the wakeword service

## Sources

- Seeed ReSpeaker 2-Mics Pi HAT overview: https://wiki.seeedstudio.com/ReSpeaker_2_Mics_Pi_HAT/
- Seeed Raspberry Pi getting started guide: https://wiki.seeedstudio.com/ReSpeaker_2_Mics_Pi_HAT_Raspberry/
