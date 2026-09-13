# Home Assistant integration

> 🇭🇷 [Hrvatska verzija](../hr/home-assistant.md)

Home Assistant runs on its own Raspberry Pi 5 (Home Assistant OS) and owns the
devices. Jarvis integrates with it in both directions: it reads and controls the
house through Home Assistant, and Home Assistant uses Jarvis as its voice
assistant and shows Jarvis's health on the dashboard.

---

## What runs where

| On the Home Assistant Pi | Purpose |
|--------------------------|---------|
| Mosquitto add-on | MQTT broker for the ESP32 relays and Jarvis's own entities |
| ESPHome | the two ESP32 nodes (native API) |
| Android TV Remote + Google Cast | the TCL TV |
| Tailscale add-on (`serve`) | real HTTPS for the phone outside the house |
| Custom integration `jarvis` | Jarvis as the Assist conversation agent |
| Wyoming integration | Jarvis's speech-to-text and text-to-speech |
| Dashboards | generated from this repository |

---

## Jarvis as the voice of Home Assistant

Covered in detail in the [voice pipeline](voice-pipeline.md#through-home-assistant-assist).
In short, `deploy/home_assistant/custom_components/jarvis/` provides:

- `conversation.py` — a `ConversationEntity` that forwards to Jarvis's
  `/api/chat`, maps Home Assistant's conversation id to a Jarvis session, and
  strips markdown and emoji before the answer is spoken.
- `config_flow.py` — setup validates the address and token against
  `/api/status` before saving; an options flow exposes the timeouts below.
- `assist_patience.py` — raises Assist's end-of-speech pause to 3 s and the
  turn limit to 30 s, safely and reversibly.
- `history.py`, `sensor.py` — `sensor.jarvis_razgovor`, the last question as
  state and the last ten turns as attributes, persisted across restarts.

`adk-wyoming` adds `stt.jarvis_stt` and `tts.jarvis_tts`. An Assist pipeline
named *Jarvis* combines all three.

Setup steps, in Croatian, are in
[`deploy/home_assistant/JARVIS_ASSIST.md`](../../deploy/home_assistant/JARVIS_ASSIST.md).

---

## Jarvis reading and controlling the house

### Lights, sockets, dimmer, scenes — MQTT

`tools/adk_tools/mqtt_adk_tools.py` switches the ESP32 relays directly over
MQTT: 15 lights, 14 sockets, a dimmer and named scenes. Every command waits up
to three seconds for the device to echo its new state. Three loads — the
fridge, the boiler and the oven — need an explicit confirmation before they are
switched in the risky direction, and the voice fast path handles plain on/off
commands without a model call.

### Sensors and their history — REST and WebSocket

`tools/adk_tools/ha_sensor_tools.py` reads temperature, humidity, pressure, air
quality and power without ever calling a Home Assistant service (a test asserts
the module never touches `/api/services`). Sensors are classified by
`device_class`, not by name, so a new node needs no code change; the room is
derived by stripping the device-name prefix and matching Croatian word endings,
so "vanjska" finds "Vanjski tlak".

History ("koja je danas bila najviša temperatura") comes from Home Assistant's
long-term statistics, which are reachable **only over the WebSocket API**. The
tool asks for hourly periods: at daily resolution a single glitched sample
would wipe out the real maximum for the whole day, at hourly resolution it costs
one hour. Readings outside the physically plausible range are dropped and
counted back to the user.

### The TV

The TCL Google TV exposes two Home Assistant entities for the same set: Android
TV Remote (keys, power, launching apps) and Google Cast (absolute volume,
casting). What was measured, not assumed:

- The remote entity has no absolute volume — `volume_set` returns HTTP 500 — so
  "glasnoća na 40 %" steps up or down while re-reading the level, and reports
  when it could not land exactly.
- YouTube search opens the first result directly (`watch?v=`), which the TV app
  autoplays.
- Apps without a deep link cannot be opened by any URI. A 12.6 KB Android TV
  app in `deploy/tv_app_launcher/` registers `jarvis://open?pkg=<package>` and
  opens any installed app ([story](engineering-notes.md#http-200-meant-nothing-opening-an-app-on-the-tv)).
- Every launch is verified by watching the foreground app actually change.
- Channels are learned by name → number and typed as remote keys, after a
  warm-up that the A1 Xplore app needs before it accepts digits.

### The shopping list

`tools/adk_tools/ha_shopping_tools.py` works on Home Assistant's `todo` entity:
add, show, mark bought, remove, and "kupio sam sve osim mlijeka".

### Everything else Home Assistant knows

`tools/adk_tools/ha_insight_tools.py` gives the agent the rest of Home
Assistant, read-only:

| Tool | Answers |
|------|---------|
| `ha_house_overview` | what is on, open or playing in each room, and what is unavailable |
| `ha_find_entities` | any entity by name, room, domain or state |
| `ha_entity_details` | every attribute of one entity |
| `ha_state_history` | when something switched on and off, and for how long |
| `ha_logbook` | what happened in the house, newest first |
| `ha_statistics` | long-term statistics; energy per hour, day, week or month |
| `ha_system_log` | Home Assistant's own warnings and errors |
| `ha_system_health` | version, failed integrations, unavailable entities, updates, log summary |

The module never changes anything. REST is read with GET, the only POST renders
a template, and WebSocket commands must pass an allow-list of four read
commands. A test checks the module's source for exactly that, because the token
that reads the logbook could also unlock a door. Home Assistant returns UTC; the
tools answer in local time.

### Notifications

Answers that outlive the Assist window arrive through `notify.<phone>`.

---

## Home Assistant watching Jarvis

`services/host_metrics.py` publishes the Jarvis Pi's CPU, memory, disk,
temperature, load, boot time and IP address through MQTT discovery, as device
`jarvis_pi_host`. It registers an MQTT last will: if the web service dies or
the network drops, the broker marks every sensor unavailable within seconds —
measured at 3 s to go unavailable and 11 s to come back.

`services/ha_mqtt_bridge.py` adds the voice entities: an online indicator, the
voice mode, the last transcript and answer, and `switch.jarvis_slusanje`, which
opens and closes the Pi's microphone.

Both clients connect asynchronously and republish discovery on every
reconnect: the Jarvis Pi boots faster than the broker on the other Pi, and a
one-shot `connect()` once left the sensors `unknown` for hours.

---

## Dashboards and the wall panel

Dashboards are **generated, not hand-edited**.
`deploy/home_assistant/tools/build_dashboard.py` reads the entity and area
registries over the WebSocket API, backs up the current configuration, and
writes the `jarvis-dom` dashboard: overview, rooms (built from the area
registry, so a new sensor appears on its own), climate, air, devices, TV,
system, energy and the conversation transcript. Lights and sockets are ordered
by how often they were actually switched on over the last 14 days — counting
`off → on` only, since a node reboot makes every socket look busy.

`deploy/home_assistant/www/` holds two front-end modules:

- `jarvis-sky.js` paints the dashboard background from the real sky: sun
  position and elevation from `sun.sun`, moon position and phase computed in
  the browser, cloud cover and precipitation from the weather entity, and CSS
  rain or snow that honours `prefers-reduced-motion`.
- `jarvis-energy-card.js` shows house power and energy on the panel.

The wall panel is the Jarvis Pi's own display: Chromium in kiosk mode on
Wayland (`deploy/rpi/panel-kiosk.sh`), with its own profile so it stays logged
in, and a secure-context exception so the "Pitaj Jarvisa" button can open the
microphone over the LAN.

---

## Useful facts about Home Assistant's APIs

Collected while building this; none of it is obvious from the documentation.

- Config entries can be created over REST; Assist pipelines, recorder
  statistics and dashboards only over WebSocket.
- `config_entries/delete` is REST-only.
- A new dashboard's `url_path` must contain a hyphen.
- WebSocket message ids must strictly increase within a connection.
- Files added to `/config/www` return 404 until Home Assistant restarts.
- An integration sensor restores its value by entity id: delete and recreate
  one under the same name and it silently resumes the old total.
- Supervisor rejects partial add-on option updates; send the whole options
  object.
