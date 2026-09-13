# Engineering notes

> 🇭🇷 [Hrvatska verzija](../hr/inzenjerske-biljeske.md)

A system that runs a real house fails in ways no test anticipated. These are
the incidents and decisions that shaped the code, each written down while it
was fresh: what was seen, what was actually wrong, what changed, and what it
taught. Dates are when it happened.

---

## The bot that was dead for two days while systemd said "running"

**2026-08-18.** The Telegram bot stopped answering. `systemctl` reported
`active (running)` for two days.

**What was wrong.** At boot, `get_me()` timed out because the network was not
really up yet despite `After=network-online.target`. The cleanup path then
called `updater.stop()` on an updater that had never started, which raised and
**replaced the real error** in the log. `main()` called `sys.exit(1)` — and the
process did not exit, because Google Cloud Logging's handler owns a non-daemon
thread that hung trying to flush 252 pending log entries. `Restart=always` never
fired. The signature, once known, is unmistakable: a live PID, eight threads,
and zero sockets.

**What changed.** `utils/process_guard.py` adds `hard_exit`: a bounded log
flush and then `os._exit`, so no thread can veto the exit. Teardown steps are
state-checked, exception-guarded and time-bounded, so cleanup can never mask
the error that caused it. The idle loop became a supervisor that raises when
polling stops or three consecutive `getMe` pings fail, and every service feeds
the systemd watchdog. Proven on the Pi: a bogus token now exits with code 1 in
8 seconds with the true error preserved.

**Lesson.** A live PID is not evidence of a working service. Make the process
prove it is serving, and make death unstoppable.

---

## Three days of "U redu" to a device that was not there

**2026-09-06.** The kitchen light stopped responding to voice. Jarvis confirmed
every command.

**What was wrong.** The ESP32 relay board had rebooted three days earlier and
never reconnected to MQTT. Home Assistant still reached it over the native
ESPHome API, so the wall panel worked and nothing looked wrong. Jarvis talks to
the relays over MQTT only. Worse, `esp32-io/status = online` is a *retained*
message; a clean disconnect does not fire the last will, so the broker kept
saying "online" forever. The confirmation code did separate a real device echo
from a retained message that merely matched — then the summary added both
together and called the total "confirmed", and the terse voice mode flattened
every result to "U redu."

**What changed.** The distinction between *confirmed*, *already in that state*
and *no answer* now reaches the user's ears. "U redu." and silence are reserved
for a verified change; failures are always spoken.

**Lesson.** Two transports to one device fail independently. And a retained
MQTT message is a memory of the past, not a statement about the present — a
live device is one that emits telemetry now.

---

## The clock that stopped at boot

**2026-08-20.** Asked at 20:29 to do something "in two minutes", the scheduler
agent produced 20:25 — which the new trigger validation correctly refused as
being in the past.

**What was wrong.** Twelve agents substituted the current date and time into
their instructions **once, when the agent was created**. Invisible in a
command-line run; badly wrong in a bot that stays up for days.

**What changed.** ADK accepts a callable as an instruction. The factory now
wraps any instruction containing date placeholders in a provider that renders
the clock on every invocation. The static part of the prompt stays above a
cache break, so rendering the time does not invalidate the prompt cache.

---

## A tool that never worked in production

**2026-08-23.** The first question through Home Assistant Assist about the
day's highest temperature failed.

**What was wrong.** `home_climate_history` called `asyncio.run()`, which raises
inside a running event loop — and the web API, the Telegram handler and the
voice loop all call tools from async code. It had only ever been tested by hand
from `python -c`, the one context nothing in production uses.

**What changed.** A bridge runs the coroutine in its own thread when a loop is
already running, and a test calls the tool from inside `asyncio.run()`.

**Lesson.** Verify a tool in the runtime that calls it, not in isolation.

---

## A research question that cost $6.28

**2026-09-03.** One deep question about heat-pump prices on Claude Opus: 13
minutes, 8 researcher calls, 1.18 million input tokens — only 43,000 of them
served from cache.

**What was wrong.** Not the model — the ReAct loop. Every tool call re-sent the
whole conversation, including the full text of every page scraped so far, and
the cache breakpoint marked only the system prompt.

**What changed.** A second cache breakpoint at the end of the conversation
prefix; scraped pages capped at 12,000 characters; and a split — the researcher
searches on Sonnet, a tool-less `synthesizer` writes the report on the stronger
model.

**Lesson.** Before raising the model of an agent with tools, count how many
times its loop re-sends the context.

---

## The wake word that went deaf

**2026-07-12.** Six attempts in a row, no reaction.

**What was wrong.** Three defences stacked against false wakes: threshold 0.40,
two consecutive frames, Silero VAD at 0.5. The VAD killed it — a quietly spoken
"hej Jarvis" peaks at 3,000–7,000 of 32,768 at the chosen capture gain, Silero
called that non-speech, and every score was clamped to zero. The heartbeat log
showed it: speech-level microphone peaks with a last score of exactly 0.000.

**What changed.** VAD off, one frame, threshold 0.32. False wakes are handled
after transcription instead, where a transcript with no letters is discarded.

**Lesson.** Tune a detector on the person who will use it, and read what each
stage actually passes on before adding another.

---

## False wakes are an invoice

**2026-08-30.** About $30 of API credit disappeared in two days.

**What was wrong.** Ambient sound and the TV crossed the wake threshold; the
recogniser returned nonsense ("Pa da se peva."); the noise gate caught only part
of it; the rest ran full agent turns on Sonnet 5 at ~56,000 tokens each. Follow-up
mode kept the microphone open, so one false wake became several turns — 61
detections, 123 turns, ~6.9 million tokens in one day.

**What changed.** The microphone became opt-in through a Home Assistant switch
with a 30-minute auto-off, and cheap questions default to the tool-less
`voice_qa` agent instead of the orchestrator. A daily budget ceiling stops a
repeat.

---

## Home Assistant cut sentences in half

**2026-08-23.** Spoken requests arrived as their first two seconds.

**What was wrong.** Assist ends a turn after 0.7 s of silence and caps it at
15 s. Neither value is exposed for the companion app — not in the UI, not in
the pipeline record, not in the WebSocket `assist_pipeline/run` command. Holding
the mic button changes nothing.

**What changed.** The custom integration moves the dataclass defaults (3 s,
30 s) at load time, locating them by name, verifying the change took, restoring
them on unload, and doing nothing but warn if Home Assistant's shape changes.
Verified: 2.5 s pauses survive, 3.5 s pauses still end the turn.

---

## Answers that died with the phone screen

**2026-09-03.** A 29-second spoken research question was transcribed, the
orchestrator finished it three and a half minutes later — and no speech was ever
requested.

**What was wrong.** The pipeline behind the Assist window was gone: the window
dies when the screen sleeps or the app goes to the background. Proven not to be
Home Assistant's fault — a 53-second turn from a custom WebSocket client came
back complete. No timeout change could fix a listener that no longer exists.

**What changed.** Past 25 seconds the run is shielded, the turn says the work
continues, and the finished answer is delivered as a phone notification.

---

## HTTP 200 meant nothing: opening an app on the TV

**2026-08-22.** "Otvori A1 Xplore TV" reported success and did nothing.

**What was wrong.** Every launch path to the TCL Google TV returned HTTP 200.
Watching the foreground app after each attempt showed that bare package names,
`market://`, `intent://` and guessed schemes all did nothing; only real deep
links (YouTube, Netflix) worked. Reading the A1 Xplore APK's manifest settled
it: the app declares only `MAIN`/`LAUNCHER` — no `VIEW` action, no scheme. No
URI can ever open it. The TV has no network ADB.

**What changed.** A 12.6 KB Android TV app (`deploy/tv_app_launcher/`) registers
`jarvis://open?pkg=<package>` and launches any installed app, built with plain
`javac`, D8, `aapt` and `apksigner` on the Pi. `tv_open_app` now verifies a
launch by watching for an actual change of the foreground app, because Home
Assistant's `app_id` attribute goes stale. Channel switching then needed one
more discovery: digits typed into A1 Xplore are ignored until the app has been
up for ~25 seconds.

**Lesson.** When a device answers "OK" to everything, measure the effect, not
the acknowledgement. When a key sequence seems not to arrive, suspect timing
before suspecting the keys.

---

## The orchestrator upgraded a worker's hedge into a claim

**2026-08-22.** `smart_home` said "Sent channel 2 to the remote; **I cannot
confirm** the app switched." The user heard "Switched to HRT 2."

**What changed.** An explicit orchestrator rule, with this incident as its
example: "sent" never becomes "switched", "cannot confirm" never becomes done,
and an *unknown* outcome is neither success nor failure — never retried "to be
sure", because that is how one email becomes two.

---

## A runaway tool loop and a turn that never ended

**2026-07-02.** On Sonnet 5, the `scribe` agent repeated the same failing call
every ~55 seconds for over 15 minutes; the voice loop hung forever.

**What was wrong.** The tool-call JSON was truncated at a 4,096-token output cap
once adaptive thinking had spent part of it, and the voice timeout existed only
in documentation.

**What changed.** A factory-level loop guard (three identical failures
short-circuit, six abort), a real voice timeout that cancels the run and says
so, and an 8,192-token output floor for Claude agents.

---

## Philosophy mode leaked into every channel

**2026-09-02.** After one spoken question about Socrates, the whole process
answered as Socrates — on every channel — for half an hour.

**What was wrong.** The Home Assistant channel was listed among spoken channels
but missing from the routing list, so its requests skipped the voice router,
reached a legacy branch that set a process-wide mode on a philosophy keyword,
and that mode stuck.

**What changed.** Assist routes like every other voice channel; the fast
answers dropped from tens of seconds to 0.3 s for the time and 0.7 s for the
weather.

---

## Confirmation that arrived before the question

**2026-09-04.** A calendar deletion sat unexecuted through three turns; a model
re-issued a held call five times in one turn.

**What was wrong.** The gate decided to hold before applying a "yes" that had
already arrived, so the consent was recorded and thrown away. And nothing told
the model it had already asked.

**What changed.** A "yes" is banked against the exact actions the user was
shown; repeated holds escalate from a question to a hard stop that ends the
turn; the held message says plainly that this is a safety step, not a fault —
after a model described it to the user as "the system asks for confirmation in
a loop".

---

## Plausibility comes from physics, not from the datasheet

**2026-08-20.** The first version of the sensor tool flagged particulate
readings above 1,000 µg/m³ as sensor errors and said so on the dashboard. The
user pointed out that cigarette smoke in the room had driven it past 100,000.

**What changed.** The particulate range check was removed; 1,000 µg/m³ is the
limit of the SPS30's specified *accuracy*, not of reality. Temperature, humidity
and pressure checks stayed — those caught a real fault, where two BME280s on
the longest cable runs intermittently returned their power-on register values
(188.5 °C and −57.6 °C, always the same two numbers per sensor). History queries
use hourly statistics so one bad sample costs an hour, not a day.
