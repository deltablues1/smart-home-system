# Hardware

> 🇭🇷 [Hrvatska verzija](../hr/hardver.md)

Everything runs on hardware in the house. No cloud VM, no hub subscription.

| Device | Role |
|--------|------|
| Raspberry Pi 5 — **Jarvis** | agents, voice, API, wall panel display |
| WM8960 audio HAT | microphone and speaker for the wake-word loop |
| Touch display on the Jarvis Pi | wall panel (Chromium kiosk) |
| Raspberry Pi 5 — **Home Assistant** | Home Assistant OS, MQTT broker, ESPHome |
| ESP32 — **I/O node** | relays for lights, sockets and boiler; dimmer; wall push-buttons |
| ESP32 — **sensor node** | five BME280 climate sensors, SPS30 air quality, power measurement |
| TCL Google TV | controlled through Home Assistant |

All four network devices use static addresses configured **on the devices
themselves**, not DHCP reservations — which is why reservations on the router
never appeared to take effect.

---

## Jarvis Pi audio

The WM8960 powers up with its output mixers routed off and a capture gain that
makes speech unrecognisable. `deploy/rpi/setup_audio.sh` sets a known-good
mixer. Two details cost real debugging time:

- The raw card rejects the 24 kHz TTS audio, so playback uses ALSA's `default`
  device, whose plug layer resamples.
- Capture at ~50 % with no input boost gives clean transcripts. Full boost clips
  and the recogniser returns nothing; too little and it mishears.

The amplifier also powers down when idle and swallows the first word of the next
sentence, which is why the microphone opens with a short spoken cue.

---

## ESP32 sensor node (`esphome/bme280-mux-node.yaml`)

```mermaid
flowchart LR
    ESP[ESP32]
    ESP -- "I2C A, GPIO21/22, 400 kHz" --> ADS1[ADS1115 0x48<br/>2 current channels]
    ESP -- "I2C A" --> ADS2[ADS1115 0x49<br/>mains voltage]
    ESP -- "I2C B, GPIO18/19, slow" --> MUX[TCA9548A 0x70]
    MUX --> B0[BME280 outdoor]
    MUX --> B1[BME280 entrance]
    MUX --> B2[BME280 living area]
    MUX --> B3[BME280 bathroom]
    MUX --> B4[BME280 bedroom]
    MUX --> SPS[SPS30 particulates]
    ADS1 --- CT1[SCT-013-000<br/>upstairs feed]
    ADS1 --- CT2[SCT-013-000<br/>incomer]
    ADS2 --- ZMPT[ZMPT101B]
    ADS1 -. RDY GPIO33 .-> ESP
    ADS2 -. RDY GPIO32 .-> ESP
```

**Two I²C buses, on purpose.** The power ADCs sample continuously at 860 SPS
and need a fast, quiet bus. The climate sensors hang off 5–10 m cable runs,
which exceed I²C's capacitance budget at speed, so they get a separate slow bus
with strong pull-ups and twisted pairs carrying a ground next to each signal.
Measured before the split: the SPS30 does not answer at 400 kHz at all, and at
100 kHz a shared bus lost 7 % of power samples.

**Five identical sensors behind a multiplexer.** Every BME280 has address
`0x76`, so each sits on its own TCA9548A channel. Swapping two cables would make
the rooms silently trade names — no firmware can notice — so the cables are
labelled and the pinout records which channel is which room.

### Power measurement: the `phase_power` component

`esphome/components/phase_power/` is a custom ESPHome component in C++. It
pairs current and voltage samples from two ADS1115 chips, driven by their
ALERT/RDY interrupts, interpolates the voltage to each current sample's instant,
and computes RMS voltage and current, real and apparent power, and power factor
per channel. The two current channels alternate on one ADC in synchronised
batches.

- The incomer clamp measures the whole house, the second clamp the upstairs
  feed; the ground floor is published as the difference of **real power only**
  — RMS current and apparent power are not additive across feeds.
- Energy totals also integrate on the ESP32 itself, so a Wi-Fi drop or a
  reflash does not leave a hole in the day.

Two lessons from commissioning it:

- **A floating pin reads half.** For a day the upstairs channel read exactly
  half of every load step (56 steps, ratio 2.00, no phase difference). New
  clamps changed nothing. The channel was measured against an ADC input that
  had no wire; switching the multiplexer to the input that actually carries the
  bias voltage fixed it. A calibration factor of 2 would have frozen the wiring
  fault into a constant.
- **Phase correction, from the meter.** Against the utility meter the house read
  6 % low overnight. Fifty-six resistive load steps (boiler, kettle) showed
  reactive power where there should be none — an apparent angle of ~7.4° —
  and re-integrating the night with a smaller angle closed the gap. The voltage
  lag correction moved from −1280 to −1690 µs.

Detailed wiring and the design notebook, in Croatian:

- [`esphome/PINOUT.md`](../../esphome/PINOUT.md) — as-built pinout, verified on
  the device.
- [`esphome/HARDVER-mjerenje-snage.md`](../../esphome/HARDVER-mjerenje-snage.md)
  — the power-measurement design and the reasoning behind each choice. Parts of
  it are a plan that was later revised (the burden resistor stayed at 47 Ω; a
  third ADC was not fitted); `PINOUT.md` and the YAML are authoritative.

### Sensor faults found in software

- Two BME280s on the longest runs intermittently returned their power-on
  register values — always the same two numbers per sensor (188.5 °C and
  −57.6 °C, humidity exactly 0 % or 100 %). Range filters now drop them on the
  ESP32, and the history tool drops them again on read.
- The SPS30's particulate floor climbed from single digits to thousands within
  days. A fan-cleaning cycle changed nothing. Rather than open a welded optical
  sensor, the recommendation was to measure the 5 V supply at the far end of the
  cable first, since a sagging supply slows the fan and inflates counts exactly
  like this.

---

## ESP32 I/O node

Relays for 15 lights, 14 sockets and the boiler, a dimmer, and 16 wall
push-buttons. Home Assistant talks to it over the native ESPHome API; Jarvis
talks to it over MQTT, which makes the two paths fail independently
([the incident](engineering-notes.md#three-days-of-u-redu-to-a-device-that-was-not-there)).

> Its ESPHome configuration is not yet in this repository.
