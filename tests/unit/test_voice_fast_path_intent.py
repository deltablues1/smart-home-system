"""
Unit tests for smart-home/voice safety (Phase A2):

- fast-path intent gate: questions, negations and deferred requests must NOT
  fire device actions from the substring shortcut
- protected devices need confirm=True (fridge/boiler OFF, oven ON)
- film scene turns off other outlets too (matching the prompt), never
  the protected ones
- noise-gate catches STT placeholders like "[nečujno]"
- unknown STT_ENGINE fails loudly instead of silently using Gemini

No network, no broker — MQTT clients are faked.

Run with:
    pytest tests/unit/test_voice_fast_path_intent.py -v
"""

import asyncio

import pytest

import services.voice_fast_path as voice_fast_path
from services.voice_fast_path import (
    classify_fast_path_intent,
    execute_fast_smart_home_command,
    normalize_voice_text,
)


# ---------------------------------------------------------------------------
# Intent gate
# ---------------------------------------------------------------------------

class TestIntentGate:
    @pytest.mark.parametrize("text", [
        "Koji film preporučuješ?",
        "Koji film preporučuješ",
        "Kakvo je stanje u kuhinji",
        "Je li upaljeno svjetlo u kuhinji",
        "Da li je ugašeno sve",
        "Nemoj ugasiti sve",
        "Nemoj upaliti svjetlo u kuhinji",
        "Ne gasi svjetlo u kuhinji",
        "Sutra ugasi sve",
        "Ugasi sve za sat vremena",
        "Kad odem, ugasi sve",
        "Ako nikoga nema, ugasi sve",
        "Podsjeti me da ugasim bojler",
        "Zakaži gašenje svih svjetala",
        # review phrases: deferred/negated/questions without "?"
        "Večeras ugasi sve",
        "Prekosutra ugasi sve",
        "U 22 sata ugasi sve",
        "U 7:30 upali svjetlo u kuhinji",
        "Nikad ugasi sve",
        "Jesu li upaljena svjetla u kuhinji",
        "Može li se ugasiti svjetlo u kuhinji",
        "Možeš li ugasiti sve kad završi film",
        "Hoćeš li upaliti svjetlo",
    ])
    def test_blocked_phrases(self, text):
        assert classify_fast_path_intent(normalize_voice_text(text)) == "blocked"

    @pytest.mark.parametrize("text", [
        "Upali svjetlo u kuhinji",
        "Ugasi sve",
        "Idemo gledati film",
        "Ugasi svjetlo u hodniku",
        "Uključi svjetlo na terasi",
    ])
    def test_commands_pass(self, text):
        assert classify_fast_path_intent(normalize_voice_text(text)) == "command"

    @pytest.mark.parametrize("text", [
        # scene alias inside an unrelated sentence must NOT fire the scene
        "Objasni film Inception",
        "Volim film Titanik",
        # compound / conflicting commands the simple matcher can't execute
        "Ugasi sve osim kuhinje",
        "Ugasi sve i upali kuhinju",
        "Upali pa ugasi svjetlo u kuhinji",
        "Za dvije minute ugasi sve",
        'Reci "upali svjetlo u kuhinji" na engleskom',
        # LIVE incident 2026-07-12: compound + dim-level executed only half
        "Ugasi svjetlo u dnevnom boravku, a stavi svjetlo u fotelju na 60%",
        "Stavi fotelju na 60 posto",
        "Upali svjetlo u kuhinji na pola",
    ])
    def test_review_round2_blocked(self, text):
        assert classify_fast_path_intent(normalize_voice_text(text)) == "blocked"

    @pytest.mark.parametrize("text", [
        "film",                 # exact alias = deliberate scene call
        "Idemo gledati film",   # activation verb present
        "Došao sam",
        "Kuham",
    ])
    def test_scene_phrases_still_pass(self, text):
        assert classify_fast_path_intent(normalize_voice_text(text)) == "command"

    def test_sent_status_spoken_distinctly(self, monkeypatch):
        # Confirm layer disabled ("sent") must not SOUND like verified success.
        async def fake_switch(device_name, state):
            return {"status": "sent"}

        monkeypatch.setattr(voice_fast_path, "mqtt_switch_control", fake_switch)
        monkeypatch.setenv("VOICE_SMART_HOME_RESPONSE_MODE", "none")

        result = asyncio.run(
            execute_fast_smart_home_command("Upali svjetlo u kuhinji")
        )
        assert result is not None
        assert "potvrda stanja je isključena" in result
        assert "Ukljucio sam" not in result

    def test_no_imperative_verb_blocked(self):
        # Positive grammar: without a command verb or scene alias, nothing fires
        assert classify_fast_path_intent(
            normalize_voice_text("svjetla u kuhinji")
        ) == "blocked"

    def test_overlong_sentence_blocked(self):
        text = ("upali svjetlo u kuhinji ali samo ako je vani mrak i "
                "nitko nije u dnevnom boravku niti na terasi")
        assert classify_fast_path_intent(normalize_voice_text(text)) == "blocked"

    def test_persona_wrapped_text_is_blocked(self):
        # Documents WHY the persona wrapper must never enter the routing path:
        # its own text trips the gate. The pipeline passes raw transcripts.
        from config.voice_persona import wrap_agent_voice_message

        wrapped = wrap_agent_voice_message("Upali svjetlo u kuhinji")
        assert classify_fast_path_intent(normalize_voice_text(wrapped)) == "blocked"

    def test_question_never_fires_device(self, monkeypatch):
        called = {"scene": False, "switch": False}

        async def fake_scene(scene):
            called["scene"] = True
            return {"status": "ok"}

        async def fake_switch(device_name, state):
            called["switch"] = True
            return {"status": "ok"}

        monkeypatch.setattr(voice_fast_path, "mqtt_scene_control", fake_scene)
        monkeypatch.setattr(voice_fast_path, "mqtt_switch_control", fake_switch)

        result = asyncio.run(
            execute_fast_smart_home_command("Koji film preporučuješ?")
        )
        assert result is None
        assert called == {"scene": False, "switch": False}

    def test_negation_never_fires_device(self, monkeypatch):
        called = {"scene": False}

        async def fake_scene(scene):
            called["scene"] = True
            return {"status": "ok"}

        monkeypatch.setattr(voice_fast_path, "mqtt_scene_control", fake_scene)

        result = asyncio.run(
            execute_fast_smart_home_command("Nemoj ugasiti sve")
        )
        assert result is None
        assert called["scene"] is False

    def test_plain_command_still_works(self, monkeypatch):
        async def fake_switch(device_name, state):
            assert device_name == "svjetlo_kuhinja"
            assert state == "ON"
            return {"status": "confirmed"}

        monkeypatch.setattr(voice_fast_path, "mqtt_switch_control", fake_switch)

        result = asyncio.run(
            execute_fast_smart_home_command(
                "Upali svjetlo u kuhinji", response_mode="full"
            )
        )
        assert result is not None and "kuhinji" in result

    def test_default_response_mode_is_ok(self, monkeypatch):
        monkeypatch.delenv("VOICE_SMART_HOME_RESPONSE_MODE", raising=False)
        assert voice_fast_path.resolve_voice_smart_home_response("Nešto") == "U redu."


# ---------------------------------------------------------------------------
# Protected devices
# ---------------------------------------------------------------------------

class TestProtectedDevices:
    @pytest.fixture(autouse=True)
    def clean_approvals(self):
        from services import approvals

        approvals.reset()
        yield
        approvals.reset()

    @pytest.mark.parametrize("device,state", [
        ("uticnica_frizider", "OFF"),
        ("uticnica_bojler", "OFF"),
        ("pecnica", "ON"),
    ])
    def test_protected_needs_confirmation(self, device, state):
        from tools.adk_tools.mqtt_adk_tools import mqtt_switch_control

        result = asyncio.run(mqtt_switch_control(device, state))
        assert result["status"] == "needs_confirmation"

    def test_same_turn_confirm_rejected(self):
        # confirm=True without a user turn in between must NOT execute —
        # the model cannot self-approve.
        from tools.adk_tools.mqtt_adk_tools import mqtt_switch_control

        first = asyncio.run(mqtt_switch_control("uticnica_bojler", "OFF"))
        assert first["status"] == "needs_confirmation"

        same_turn = asyncio.run(
            mqtt_switch_control("uticnica_bojler", "OFF", confirm=True)
        )
        assert same_turn["status"] == "needs_confirmation"

    def test_cold_confirm_rejected(self):
        # confirm=True out of nowhere (no prior needs_confirmation) is rejected.
        from tools.adk_tools.mqtt_adk_tools import mqtt_switch_control

        result = asyncio.run(
            mqtt_switch_control("uticnica_frizider", "OFF", confirm=True)
        )
        assert result["status"] == "needs_confirmation"

    def test_approval_expires(self, monkeypatch):
        import time as time_mod

        import tools.adk_tools.mqtt_adk_tools as mqtt_tools
        from services import approvals

        asyncio.run(mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF"))
        assert approvals.has_pending("global") is True

        # Simulate TTL expiry: an approval the user never got back to must not
        # still be sitting there an hour later waiting for a stray "da".
        key = ("global", mqtt_tools._action_id("uticnica_bojler", "OFF"))
        approvals._PENDING[key].created_at = time_mod.monotonic() - 999

        assert approvals.has_pending("global") is False
        mqtt_tools.arm_pending_approvals("global")  # purges expired
        assert key not in approvals._PENDING

    def test_other_session_cannot_arm(self):
        # An approval pending in session A must not be unlocked by traffic
        # in session B.
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        mqtt_tools.set_approval_session("session-a")
        first = asyncio.run(mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF"))
        assert first["status"] == "needs_confirmation"

        mqtt_tools.arm_pending_approvals("session-b")  # wrong session

        blocked = asyncio.run(
            mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF", confirm=True)
        )
        assert blocked["status"] == "needs_confirmation"
        mqtt_tools.set_approval_session("global")

    def test_negative_reply_cancels(self):
        # cancel_pending_approvals ("ne"/unrelated message) kills the approval.
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        asyncio.run(mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF"))
        assert mqtt_tools.has_pending_approval("global") is True

        mqtt_tools.cancel_pending_approvals("global")
        assert mqtt_tools.has_pending_approval("global") is False

        blocked = asyncio.run(
            mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF", confirm=True)
        )
        assert blocked["status"] == "needs_confirmation"

    @pytest.mark.parametrize("device,state", [
        ("uticnica_frizider", "ON"),   # turning fridge ON is safe
        ("pecnica", "OFF"),            # turning oven OFF is safe
        ("svjetlo_kuhinja", "OFF"),
    ])
    def test_unprotected_publishes(self, device, state, monkeypatch):
        import services.mqtt_confirm as mqtt_confirm
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        captured = {}

        async def fake_confirm(commands, timeout=None):
            captured["payload"] = commands[0].payload
            return {
                "status": "confirmed", "operation_id": "op1",
                "confirmed": 1, "total": 1,
                "devices": {commands[0].name: {"status": "confirmed", "observed": state}},
            }

        monkeypatch.setattr(mqtt_confirm, "publish_and_confirm", fake_confirm)

        result = asyncio.run(mqtt_tools.mqtt_switch_control(device, state))
        assert result["status"] == "confirmed"
        assert captured["payload"] == state

    def test_confirmed_protected_publishes_after_user_turn(self, monkeypatch):
        # Full turn-gated cycle: needs_confirmation -> user's explicit "da"
        # arms the approval -> confirm=True executes.
        import services.mqtt_confirm as mqtt_confirm
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        captured = {}

        async def fake_confirm(commands, timeout=None):
            captured["payload"] = commands[0].payload
            return {
                "status": "confirmed", "operation_id": "op1",
                "confirmed": 1, "total": 1,
                "devices": {commands[0].name: {"status": "confirmed", "observed": "OFF"}},
            }

        monkeypatch.setattr(mqtt_confirm, "publish_and_confirm", fake_confirm)

        first = asyncio.run(mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF"))
        assert first["status"] == "needs_confirmation"

        mqtt_tools.arm_pending_approvals("global")  # user said "da" in a new turn

        result = asyncio.run(
            mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF", confirm=True)
        )
        assert result["status"] == "confirmed"
        assert captured["payload"] == "OFF"

        # approval was consumed — a repeat needs a fresh confirmation
        repeat = asyncio.run(
            mqtt_tools.mqtt_switch_control("uticnica_bojler", "OFF", confirm=True)
        )
        assert repeat["status"] == "needs_confirmation"


# ---------------------------------------------------------------------------
# Scene composition
# ---------------------------------------------------------------------------

class TestSceneComposition:
    def _run_scene(self, monkeypatch, scene):
        import services.mqtt_confirm as mqtt_confirm
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        captured = {}

        async def fake_confirm(commands, timeout=None):
            captured["commands"] = commands
            return {
                "status": "confirmed", "operation_id": "op1",
                "confirmed": len(commands), "total": len(commands),
                "devices": {
                    c.name: {"status": "confirmed", "observed": c.payload}
                    for c in commands
                },
            }

        monkeypatch.setattr(mqtt_confirm, "publish_and_confirm", fake_confirm)
        result = asyncio.run(mqtt_tools.mqtt_scene_control(scene))
        return result, captured["commands"]

    def test_film_turns_off_other_outlets(self, monkeypatch):
        result, commands = self._run_scene(monkeypatch, "film")
        assert result["status"] == "confirmed"
        topics_off = [c.command_topic for c in commands if c.payload == "OFF"]
        topics_on = [c.command_topic for c in commands if c.payload == "ON"]

        assert "esp32-io/switch/uticnica_boravak/command" in topics_off
        assert "esp32-io/switch/uticnica_tv/command" in topics_on
        assert "esp32-io/switch/svjetlo_tv/command" in topics_on
        # protected devices untouched
        all_topics = [c.command_topic for c in commands]
        assert "esp32-io/switch/uticnica_frizider/command" not in all_topics
        assert "esp32-io/switch/uticnica_bojler/command" not in all_topics

    def test_sve_ugasi_spares_protected(self, monkeypatch):
        result, commands = self._run_scene(monkeypatch, "sve_ugasi")
        assert result["status"] == "confirmed"
        assert result["summary"].endswith("potvrđeno")
        all_topics = [c.command_topic for c in commands]
        assert "esp32-io/switch/uticnica_frizider/command" not in all_topics
        assert "esp32-io/switch/uticnica_bojler/command" not in all_topics
        assert "esp32-io/switch/svjetlo_kuhinja/command" in all_topics


# ---------------------------------------------------------------------------
# Turn-approval semantics in the interface (da/ne parsing)
# ---------------------------------------------------------------------------

class TestTurnApprovalSemantics:
    @pytest.fixture(autouse=True)
    def clean_approvals(self):
        from services import approvals

        approvals.reset()
        yield
        approvals.reset()

    @pytest.fixture
    def iface(self):
        from types import SimpleNamespace

        from interfaces.base_interface import BaseInterface

        class _Iface(BaseInterface):
            def __init__(self):
                super().__init__(session_prefix="test")
                self.system = SimpleNamespace(philosophy_keywords=set())

            async def start(self):  # pragma: no cover
                pass

            async def stop(self):  # pragma: no cover
                pass

            def format_response(self, response):  # pragma: no cover
                return response

        return _Iface()

    def _register(self, session="sess-1"):
        import tools.adk_tools.mqtt_adk_tools as mqtt_tools

        mqtt_tools.set_approval_session(session)
        mqtt_tools.register_pending_approval("uticnica_bojler", "OFF")
        return mqtt_tools

    def test_da_arms_approval(self, iface):
        mqtt_tools = self._register()
        iface._process_turn_approvals("sess-1", "da")
        assert mqtt_tools.redeem_approval("uticnica_bojler", "OFF") is True

    def test_ne_cancels_approval(self, iface):
        mqtt_tools = self._register()
        iface._process_turn_approvals("sess-1", "ne")
        assert mqtt_tools.has_pending_approval("sess-1") is False

    def test_unrelated_message_cancels(self, iface):
        mqtt_tools = self._register()
        iface._process_turn_approvals("sess-1", "koliko je sati u Tokiju")
        assert mqtt_tools.has_pending_approval("sess-1") is False

    def test_pending_approval_sets_one_shot_pin(self, iface):
        self._register()
        iface._process_turn_approvals("sess-1", "da")
        # short reply gets routed back to smart_home via the pin
        route = iface._apply_voice_lane_pin("sess-1", "da", "agent", "voice_qa")
        assert route == ("agent", "smart_home")

    def test_no_pin_without_pending_approval(self, iface):
        iface._process_turn_approvals("sess-1", "da")
        route = iface._apply_voice_lane_pin("sess-1", "da", "agent", "voice_qa")
        assert route == ("agent", "voice_qa")


# ---------------------------------------------------------------------------
# Noise gate placeholders
# ---------------------------------------------------------------------------

class TestNoiseGate:
    @pytest.mark.parametrize("text", [
        "[nečujno]", "[necujno]", "(nerazumljivo)", "[inaudible]",
        "[glazba]", "(tišina)", "...", "3 7 1 2 0 6 8 3",
    ])
    def test_noise_flagged(self, text):
        from interfaces.wakeword_interface import looks_like_noise_transcript

        assert looks_like_noise_transcript(text) is True

    @pytest.mark.parametrize("text", [
        "Koliko je 2 i 2",
        "Upali svjetlo u kuhinji",
        "Sastanak u 21:30",
    ])
    def test_speech_passes(self, text):
        from interfaces.wakeword_interface import looks_like_noise_transcript

        assert looks_like_noise_transcript(text) is False


# ---------------------------------------------------------------------------
# STT engine dispatch
# ---------------------------------------------------------------------------

class TestSttEngineDispatch:
    def test_unknown_engine_fails_loudly(self, monkeypatch):
        from services.audio_ingress import AudioIngressError, AudioIngressService

        monkeypatch.setenv("STT_ENGINE", "whisperx")
        service = AudioIngressService(api_key="dummy")

        with pytest.raises(AudioIngressError, match="Unsupported STT_ENGINE"):
            asyncio.run(
                service.transcribe_audio(b"RIFF....", "audio/wav", "test")
            )

    @pytest.mark.parametrize("engine", ["openai", "chirp", "chirp_2", "cloud"])
    def test_stt_service_engines_delegated(self, engine, monkeypatch):
        # STT_ENGINE=openai is implemented by STTService and must be
        # delegated, not rejected (regression: it was rejected once).
        from services.audio.stt_service import STTService
        from services.audio_ingress import AudioIngressService

        monkeypatch.setenv("STT_ENGINE", engine)

        async def fake_transcribe(self, audio_bytes, mime_type):
            return "upali svjetlo"

        monkeypatch.setattr(STTService, "transcribe", fake_transcribe)

        service = AudioIngressService(api_key="dummy")
        result = asyncio.run(
            service.transcribe_audio(b"RIFF....", "audio/wav", "test")
        )
        assert result.transcript == "upali svjetlo"


class TestBrevityNeverHidesTheDifference:
    """"U redu." and silence both mean "done".

    The Pi runs VOICE_SMART_HOME_RESPONSE_MODE=ok, which answers every
    smart-home command with those two words. Right for a light that just came
    on; wrong for one that was already on — and on 2026-09-06 it flattened
    both into the same reply, so a command that changed nothing sounded
    exactly like one that worked. The state-honesty fix in mqtt_confirm was
    invisible to the person listening.
    """

    def test_a_real_success_may_be_shortened(self):
        from services.voice_fast_path import resolve_voice_smart_home_response

        assert resolve_voice_smart_home_response("Ukljucio sam svjetlo.", "ok") == "U redu."
        assert resolve_voice_smart_home_response("Ukljucio sam svjetlo.", "none") == ""

    def test_anything_else_is_spoken_in_full(self):
        from services.voice_fast_path import speak_in_full

        for mode in ("ok", "none", "full", None):
            assert speak_in_full("svjetlo u kuhinji je već upaljeno.", mode) == (
                "svjetlo u kuhinji je već upaljeno."
            )

    @pytest.mark.asyncio
    async def test_already_on_is_not_reported_as_u_redu(self, monkeypatch):
        import services.voice_fast_path as fast_path

        async def already(device_name, state):
            return {"status": "already_in_state", "devices": {}}

        monkeypatch.setattr(fast_path, "mqtt_switch_control", already)
        monkeypatch.setenv("VOICE_SMART_HOME_RESPONSE_MODE", "ok")

        said = await fast_path.execute_fast_smart_home_command(
            "upali svjetlo u kuhinji"
        )

        assert said != "U redu."
        assert "već" in said

    @pytest.mark.asyncio
    async def test_a_confirmed_change_still_gets_the_short_reply(self, monkeypatch):
        import services.voice_fast_path as fast_path

        async def confirmed(device_name, state):
            return {"status": "confirmed", "devices": {}}

        monkeypatch.setattr(fast_path, "mqtt_switch_control", confirmed)
        monkeypatch.setenv("VOICE_SMART_HOME_RESPONSE_MODE", "ok")

        said = await fast_path.execute_fast_smart_home_command(
            "upali svjetlo u kuhinji"
        )

        assert said == "U redu."
