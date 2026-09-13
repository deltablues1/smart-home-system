"""Tests for the TV tools, pinned to what the hardware actually supports.

Verified against the TCL on 2026-08-20:
- media_player.tv reports VOLUME_STEP but NOT VOLUME_SET (volume_set -> HTTP 500)
- remote.tv.activity_list is empty, so HA knows no installed apps at all
- launching by package name via remote.turn_on works
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.adk_tools import ha_adk_tools as tv  # noqa: E402


@pytest.fixture
def apps_file(tmp_path, monkeypatch):
    path = tmp_path / "tv_apps.json"
    monkeypatch.setenv("TV_APPS_FILE", str(path))
    return path


@pytest.fixture
def ha(monkeypatch):
    """Record HA service calls and serve canned states."""
    calls = []
    states = {
        "media_player.tv": {
            "state": "on",
            "attributes": {"volume_level": 0.30, "app_id": "com.google.android.apps.tv.launcherx"},
        },
        "remote.tv": {"state": "on", "attributes": {"activity_list": []}},
    }

    def fake_request(path, payload=None, timeout=10.0):
        if payload is None:
            entity = path.rsplit("/", 1)[-1]
            if entity not in states:
                raise RuntimeError(f"nema {entity}")
            return states[entity]
        calls.append((path, payload))
        # A launch command makes the TV switch app, which is what the tool
        # now verifies. Tests that want a TV which ignores commands patch
        # _wait_for_app_change instead.
        if "remote/turn_on" in path and "activity" in payload:
            activity = payload["activity"]
            # A jarvis:// link resolves to the package it carries, which is
            # what the TV then reports as the foreground app.
            if activity.startswith("jarvis://"):
                from urllib.parse import parse_qs, urlparse
                activity = parse_qs(urlparse(activity).query).get("pkg", [""])[0]
            states["media_player.tv"]["attributes"]["app_id"] = activity
        return []

    monkeypatch.setattr(tv, "_ha_request", fake_request)
    monkeypatch.setattr(tv, "_wait_for_app_change",
                        lambda before, expected="", timeout=None:
                        states["media_player.tv"]["attributes"].get("app_id"))
    return {"calls": calls, "states": states}


# --- volume ---------------------------------------------------------------

def test_volume_set_steps_instead_of_calling_unsupported_volume_set(ha):
    """volume_set returns HTTP 500 on this TV, so it must never be called."""
    current = {"value": 0.30}

    def fake_request(path, payload=None, timeout=10.0):
        if payload is None:
            return {"attributes": {"volume_level": current["value"]}}
        ha["calls"].append((path, payload))
        if "volume_up" in path:
            current["value"] = round(current["value"] + 0.05, 2)
        return []

    with patch.object(tv, "_ha_request", fake_request):
        result = tv.tv_volume("set", 50)

    services = [path for path, _ in ha["calls"]]
    assert not any("volume_set" in s for s in services)
    assert all("volume_up" in s for s in services)
    assert result["success"] is True
    assert "50" in result["detail"]


def test_volume_set_gives_up_instead_of_looping_forever(ha):
    """A TV that stops responding to steps must not spin 30 requests."""
    def stuck(path, payload=None, timeout=10.0):
        if payload is None:
            return {"attributes": {"volume_level": 0.10}}
        ha["calls"].append((path, payload))
        return []

    with patch.object(tv, "_ha_request", stuck):
        result = tv.tv_volume("set", 90)

    assert len(ha["calls"]) == 1          # level never moved -> stop at once
    assert result["exact"] is False       # and say so


def test_volume_set_reports_inexactness(ha):
    with patch.object(tv, "_ha_request", lambda p, payload=None, timeout=10.0:
                      {"attributes": {"volume_level": 0.30}} if payload is None else []):
        result = tv.tv_volume("set", 30)

    assert result["exact"] is True


def test_volume_up_still_uses_steps(ha):
    tv.tv_volume("up")
    assert all("volume_up" in path for path, _ in ha["calls"])


# --- apps -----------------------------------------------------------------

def test_unknown_app_lists_what_is_known_instead_of_pretending(ha, apps_file):
    result = tv.tv_open_app("a1 xplore tv")

    assert "error" in result
    assert "youtube" in result["poznate_aplikacije"]
    assert "zapamti ovu aplikaciju" in result["kako_dodati"]
    assert ha["calls"] == []            # nothing was launched


def test_builtin_app_launches_by_deep_link(ha, apps_file):
    result = tv.tv_open_app("netflix")

    assert result["success"] is True
    path, payload = ha["calls"][0]
    assert "remote/turn_on" in path
    assert payload["activity"].startswith("https://www.netflix.com")


def test_learning_an_app_then_launching_it(ha, apps_file):
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "hr.a1.xplore"

    learned = tv.tv_learn_app("A1 Xplore TV")
    assert learned["success"] is True
    assert learned["package"] == "hr.a1.xplore"
    assert json.loads(apps_file.read_text(encoding="utf-8")) == {"A1 Xplore TV": "hr.a1.xplore"}

    # Leave the app before launching it, otherwise there is no transition to
    # observe and the tool correctly refuses to confirm anything.
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "com.google.android.apps.tv.launcherx"

    result = tv.tv_open_app("a1 xplore tv")
    assert result["success"] is True
    assert ha["calls"][-1][1]["activity"] == "jarvis://open?pkg=hr.a1.xplore"


def test_learned_app_matches_partially(ha, apps_file):
    apps_file.write_text(json.dumps({"a1 xplore tv": "hr.a1.xplore"}), encoding="utf-8")

    result = tv.tv_open_app("a1")

    assert result["success"] is True
    assert ha["calls"][-1][1]["activity"] == "jarvis://open?pkg=hr.a1.xplore"


def test_learning_refuses_the_home_screen(ha, apps_file):
    # app_id is the launcher in the default fixture
    result = tv.tv_learn_app("A1")

    assert "error" in result
    assert not apps_file.exists()


@pytest.mark.parametrize("package", [
    "com.google.android.apps.tv.launcherx",
    "com.google.android.apps.tv.dreamx",      # screensaver, seen live
    "com.google.android.tvlauncher",
])
def test_learning_refuses_launcher_and_screensaver(ha, apps_file, package):
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = package

    result = tv.tv_learn_app("A1")

    assert "error" in result
    assert not apps_file.exists()


def test_learning_needs_a_name(ha, apps_file):
    assert "error" in tv.tv_learn_app("  ")


def test_list_apps_reports_all_three_sources(ha, apps_file):
    apps_file.write_text(json.dumps({"a1": "hr.a1.xplore"}), encoding="utf-8")
    ha["states"]["remote.tv"]["attributes"]["activity_list"] = ["Netflix"]

    listed = tv.tv_list_apps()

    assert "youtube" in listed["ugradene"]
    assert listed["naucene"] == {"a1": "hr.a1.xplore"}
    assert listed["iz_home_assistanta"] == ["Netflix"]
    assert listed["trenutno_otvorena"].endswith("launcherx")


def test_raw_package_or_url_is_launched_as_is(ha, apps_file):
    assert tv.tv_open_app("com.example.app")["success"] is True
    assert ha["calls"][-1][1]["activity"] == "jarvis://open?pkg=com.example.app"


# --- youtube --------------------------------------------------------------

def test_youtube_plays_the_first_hit_not_the_search_page(ha):
    with patch.object(tv, "_youtube_first_result", return_value=("XFkzRNyygfk", "Radiohead - Creep")):
        result = tv.tv_play_youtube("Radiohead Creep")

    assert result["playing"] is True
    assert result["naslov"] == "Radiohead - Creep"
    assert ha["calls"][-1][1]["activity"] == "https://www.youtube.com/watch?v=XFkzRNyygfk"


def test_youtube_falls_back_to_search_when_resolution_fails(ha):
    with patch.object(tv, "_youtube_first_result", return_value=(None, None)):
        result = tv.tv_play_youtube("nešto nepostojeće")

    assert result["playing"] is False
    assert "results?search_query=" in ha["calls"][-1][1]["activity"]
    assert "izaberi daljinskim" in result["detail"]


def test_youtube_search_only_mode_is_honoured(ha):
    with patch.object(tv, "_youtube_first_result") as resolver:
        result = tv.tv_play_youtube("Radiohead Creep", play_first=False)

    resolver.assert_not_called()
    assert result["playing"] is False
    assert "results?search_query=" in ha["calls"][-1][1]["activity"]


def test_youtube_parser_extracts_id_and_title():
    html = '{"videoId":"XFkzRNyygfk","title":{"runs":[{"text":"Radiohead - Creep"}]}}'

    class FakeResponse:
        def read(self):
            return html.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        video_id, title = tv._youtube_first_result("Radiohead Creep")

    assert video_id == "XFkzRNyygfk"
    assert title == "Radiohead - Creep"


def test_youtube_network_failure_is_not_an_exception():
    with patch("urllib.request.urlopen", side_effect=OSError("no network")):
        assert tv._youtube_first_result("x") == (None, None)


# --- channels -------------------------------------------------------------

@pytest.fixture
def channels_file(tmp_path, monkeypatch):
    path = tmp_path / "tv_channels.json"
    monkeypatch.setenv("TV_CHANNELS_FILE", str(path))
    monkeypatch.setenv("TV_CHANNEL_APP_DELAY_SECONDS", "0")
    return path


def test_learn_then_switch_to_channel_by_name(ha, channels_file):
    assert tv.tv_learn_channel("HRT 1", 101)["success"] is True

    result = tv.tv_channel("hrt 1")

    assert result["success"] is True
    path, payload = ha["calls"][-1]
    assert "remote/send_command" in path
    assert payload["command"] == ["1", "0", "1", "DPAD_CENTER"]


def test_channel_can_be_given_as_a_bare_number(ha, channels_file):
    result = tv.tv_channel("305")

    assert result["success"] is True
    assert ha["calls"][-1][1]["command"] == ["3", "0", "5", "DPAD_CENTER"]


def test_unknown_channel_asks_instead_of_guessing(ha, channels_file):
    tv.tv_learn_channel("HRT 1", 101)

    result = tv.tv_channel("Nova TV")

    assert "error" in result
    assert result["poznati_kanali"] == ["HRT 1"]
    assert ha["calls"] == []


def test_channel_opens_its_app_when_not_already_there(ha, channels_file, apps_file):
    apps_file.write_text(json.dumps({"a1 xplore tv": "hr.a1.xplore"}), encoding="utf-8")
    tv.tv_learn_channel("HRT 1", 101, app="a1 xplore tv")

    tv.tv_channel("HRT 1")

    launched = [p for path, p in ha["calls"] if "remote/turn_on" in path]
    assert launched and launched[0]["activity"] == "jarvis://open?pkg=hr.a1.xplore"


def test_channel_does_not_relaunch_the_app_it_is_already_in(ha, channels_file, apps_file):
    apps_file.write_text(json.dumps({"a1 xplore tv": "hr.a1.xplore"}), encoding="utf-8")
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "hr.a1.xplore"
    tv.tv_learn_channel("HRT 1", 101, app="a1 xplore tv")

    tv.tv_channel("HRT 1")

    assert not any("remote/turn_on" in path for path, _ in ha["calls"])


def test_learn_channel_rejects_nonsense(ha, channels_file):
    assert "error" in tv.tv_learn_channel("", 101)
    assert "error" in tv.tv_learn_channel("HRT 1", "abc")
    assert "error" in tv.tv_learn_channel("HRT 1", 0)
    assert not channels_file.exists()


def test_list_channels_reports_what_is_stored(ha, channels_file):
    tv.tv_learn_channel("HRT 1", 101, app="a1 xplore tv")

    listed = tv.tv_list_channels()

    assert listed["broj"] == 1
    assert listed["kanali"]["HRT 1"] == {"number": 101, "app": "a1 xplore tv"}


def test_catalogue_channel_without_a_number_asks_for_it(ha, channels_file):
    """Seeded from A1's public list, which publishes names but no numbers."""
    channels_file.write_text(
        json.dumps({"Arena Sport 1 HD": {"number": None, "app": "a1 xplore tv"}}),
        encoding="utf-8",
    )

    result = tv.tv_channel("Arena Sport 1 HD")

    assert "error" in result
    assert "ne znam njegov broj" in result["error"]
    assert ha["calls"] == []


def test_learning_a_number_keeps_the_app_from_the_catalogue(ha, channels_file):
    channels_file.write_text(
        json.dumps({"Arena Sport 1 HD": {"number": None, "app": "a1 xplore tv"}}),
        encoding="utf-8",
    )

    tv.tv_learn_channel("Arena Sport 1 HD", 204)

    stored = json.loads(channels_file.read_text(encoding="utf-8"))
    assert stored["Arena Sport 1 HD"] == {"number": 204, "app": "a1 xplore tv"}


@pytest.mark.parametrize("spoken", ["HRT 1", "hrt1", "HRT1", "hrt 1 hd", "HRT 1 HD"])
def test_channel_name_matches_however_it_is_spelled(ha, channels_file, spoken):
    channels_file.write_text(
        json.dumps({"HRT 1 HD": {"number": 1, "app": ""}}), encoding="utf-8"
    )

    result = tv.tv_channel(spoken)

    assert result["success"] is True
    assert ha["calls"][-1][1]["command"] == ["1", "DPAD_CENTER"]


def test_normalisation_does_not_merge_different_channels(ha, channels_file):
    channels_file.write_text(json.dumps({
        "Arena Sport 1 HD": {"number": 201, "app": ""},
        "Arena Sport 10 HD": {"number": 210, "app": ""},
    }), encoding="utf-8")

    tv.tv_channel("Arena sport 10")

    assert ha["calls"][-1][1]["command"] == ["2", "1", "0", "DPAD_CENTER"]


# --- routing --------------------------------------------------------------

def test_agent_descriptions_advertise_everything_smart_home_can_do():
    """The orchestrator routes on descriptions, not on tool lists.

    On 2026-08-20 it answered "Na tv otvori a1 xplore tv aplikaciju" with
    "nemam agenta koji može upravljati TV-om" — the tools existed, but every
    description still said only "lights, outlets, dimmer, scenes". A capability
    the router cannot see does not exist.
    """
    import inspect
    from pathlib import Path

    from agents.adk_agents import smart_home_adk
    from config import agent_registry

    root = Path(__file__).resolve().parents[2]
    surfaces = {
        "smart_home_adk": inspect.getsource(smart_home_adk.create_smart_home_agent),
        "agent_registry": inspect.getsource(agent_registry),
        "orchestrator instructions":
            (root / "agents" / "orchestrator" / "instructions.md").read_text(encoding="utf-8"),
    }

    for label, text in surfaces.items():
        lowered = text.lower()
        assert "tv" in lowered, f"{label} never mentions the TV"
        for capability in ("app", "channel", "kanal", "aplikacij"):
            if capability in lowered:
                break
        else:
            raise AssertionError(f"{label} mentions no apps or channels")
        assert any(word in lowered for word in ("sensor", "senzor", "temperature", "temperatura")), (
            f"{label} never mentions sensors"
        )


def test_voice_routing_catches_app_and_channel_requests():
    from interfaces.base_interface import SMART_HOME_KEYWORDS, _TV_WORD_RE

    for phrase in (
        "na tv otvori a1 xplore tv aplikaciju",
        "stavi hrt 1",
        "prebaci na arena sport 4",
        "otvori aplikaciju netflix",
    ):
        matched = any(kw in phrase for kw in SMART_HOME_KEYWORDS) or bool(_TV_WORD_RE.search(phrase))
        assert matched, f"voice routing would miss: {phrase}"


def test_status_does_not_let_seeing_an_app_pass_for_remembering_it(ha, apps_file):
    """On 2026-08-20 the agent read the package from tv_status and told the
    user the app was remembered. Nothing had been saved."""
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "hr.a1.android.tv.xploretv"

    status = tv.tv_status()

    assert status["package"] == "hr.a1.android.tv.xploretv"
    assert status["aplikacija_zapamcena"] is False
    assert "NIJE zapamćena" in status["upozorenje"]


def test_status_confirms_a_genuinely_learned_app(ha, apps_file):
    apps_file.write_text(
        json.dumps({"A1 Xplore TV": "hr.a1.android.tv.xploretv"}), encoding="utf-8"
    )
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "hr.a1.android.tv.xploretv"

    status = tv.tv_status()

    assert status["aplikacija_zapamcena"] is True
    assert status["zapamcena_kao"] == "A1 Xplore TV"
    assert "upozorenje" not in status


def test_status_does_not_nag_about_the_home_screen(ha, apps_file):
    status = tv.tv_status()   # fixture has the launcher open

    assert status["aplikacija_zapamcena"] is False
    assert "upozorenje" not in status


def test_learn_app_accepts_an_explicit_package(ha, apps_file):
    result = tv.tv_learn_app("A1 Xplore TV", package="hr.a1.android.tv.xploretv")

    assert result["success"] is True
    assert json.loads(apps_file.read_text(encoding="utf-8")) == {
        "A1 Xplore TV": "hr.a1.android.tv.xploretv"
    }


# --- launch verification --------------------------------------------------

def test_open_app_reports_failure_when_the_tv_ignores_the_command(ha, apps_file, monkeypatch):
    """HTTP 200 from Home Assistant says nothing about what the TV did.

    Measured 2026-08-22: youtube.com opened YouTube, while the A1 package name,
    market://, intent:// and play_media all returned 200 and left the TV on its
    home screen. Reporting those as success is how "rekao je da je otvorio"
    happens.
    """
    monkeypatch.setattr(tv, "_wait_for_app_change",
                        lambda before, expected="", timeout=None: None)
    apps_file.write_text(json.dumps({"a1": "hr.a1.android.tv.xploretv"}), encoding="utf-8")

    result = tv.tv_open_app("a1")

    assert "error" in result
    assert "ostao na istom ekranu" in result["error"]
    assert result["poslano"] == "jarvis://open?pkg=hr.a1.android.tv.xploretv"
    assert "success" not in result


def test_open_app_confirms_a_launch_that_really_happened(ha, apps_file):
    apps_file.write_text(json.dumps({"a1": "hr.a1.android.tv.xploretv"}), encoding="utf-8")

    result = tv.tv_open_app("a1")

    assert result["success"] is True
    assert result["app_id"] == "hr.a1.android.tv.xploretv"


def test_wait_for_app_change_requires_the_exact_package_when_asked(monkeypatch):
    seen = iter(["com.launcher", "com.launcher", "hr.a1.android.tv.xploretv"])
    monkeypatch.setattr(tv, "_ha_request",
                        lambda path, payload=None, timeout=10.0:
                        {"attributes": {"app_id": next(seen, "hr.a1.android.tv.xploretv")}})
    monkeypatch.setattr(tv.time, "sleep", lambda _s: None)

    got = tv._wait_for_app_change("com.launcher", expected="hr.a1.android.tv.xploretv", timeout=5)

    assert got == "hr.a1.android.tv.xploretv"


def test_wait_for_app_change_gives_up_and_says_nothing_happened(monkeypatch):
    monkeypatch.setattr(tv, "_ha_request",
                        lambda path, payload=None, timeout=10.0:
                        {"attributes": {"app_id": "com.launcher"}})
    monkeypatch.setattr(tv.time, "sleep", lambda _s: None)

    assert tv._wait_for_app_change("com.launcher", timeout=2) is None


def test_open_app_refuses_to_confirm_when_the_tv_already_reported_that_app(ha, apps_file, monkeypatch):
    """HA's app_id goes stale — on 2026-08-22 it claimed A1 was open for eight
    minutes while the screen showed the home screen. Matching the expected
    package without seeing a transition is therefore not evidence."""
    apps_file.write_text(json.dumps({"a1": "hr.a1.android.tv.xploretv"}), encoding="utf-8")
    ha["states"]["media_player.tv"]["attributes"]["app_id"] = "hr.a1.android.tv.xploretv"

    result = tv.tv_open_app("a1")

    assert result.get("nepotvrdivo") is True
    assert "success" not in result
    assert "ne mogu potvrditi" in result["napomena"]


def test_wait_for_app_change_ignores_a_reading_that_never_moved(monkeypatch):
    """Stale state stuck on the target package must not read as a launch."""
    monkeypatch.setattr(tv, "_ha_request",
                        lambda path, payload=None, timeout=10.0:
                        {"attributes": {"app_id": "hr.a1.android.tv.xploretv"}})
    monkeypatch.setattr(tv.time, "sleep", lambda _s: None)

    got = tv._wait_for_app_change("hr.a1.android.tv.xploretv",
                                  expected="hr.a1.android.tv.xploretv", timeout=2)

    assert got is None


def test_orchestrator_is_told_not_to_launder_hedges():
    """smart_home said "ne mogu potvrditi"; the user was told "prebacio sam"."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "agents" / "orchestrator"
            / "instructions.md").read_text(encoding="utf-8").lower()

    assert "poslao sam" in text and "prebacio sam" in text
    assert "ne mogu potvrditi" in text


def test_channel_result_states_the_live_tv_precondition(ha, channels_file):
    """Digits only reach A1 Xplore while it is showing a channel; on the app's
    own home page they are discarded (measured 2026-08-22)."""
    channels_file.write_text(
        json.dumps({"Arena Sport 1 HD": {"number": 201, "app": ""}}), encoding="utf-8"
    )

    result = tv.tv_channel("Arena Sport 1")

    assert result["broj"] == "201"
    assert "live TV" in result["napomena"]
    assert "početnoj stranici" in result["napomena"]


def test_channel_can_enter_live_tv_first_when_the_app_just_opened(ha, channels_file, monkeypatch):
    """A freshly launched A1 Xplore sits on its landing page, where digits are
    discarded. The user verified right-arrow then OK reaches live TV."""
    monkeypatch.setenv("TV_CHANNEL_LIVE_DELAY_SECONDS", "0")
    channels_file.write_text(json.dumps({"HRT 1": {"number": 1, "app": ""}}), encoding="utf-8")

    result = tv.tv_channel("HRT 1", from_app_home=True)

    sequences = [p["command"] for path, p in ha["calls"] if "send_command" in path]
    assert sequences[0] == ["DPAD_RIGHT", "DPAD_CENTER"]
    assert sequences[1] == ["1", "DPAD_CENTER"]
    assert result["iz_pocetne_stranice"] is True


def test_channel_skips_the_live_tv_step_by_default(ha, channels_file):
    channels_file.write_text(json.dumps({"HRT 1": {"number": 1, "app": ""}}), encoding="utf-8")

    result = tv.tv_channel("HRT 1")

    sequences = [p["command"] for path, p in ha["calls"] if "send_command" in path]
    assert sequences == [["1", "DPAD_CENTER"]]
    assert result["iz_pocetne_stranice"] is False


# --- proxy launcher -------------------------------------------------------

def test_deep_links_bypass_the_proxy(ha, apps_file):
    """Netflix claims its own URL; routing it through the proxy would be silly."""
    tv.tv_open_app("netflix")

    assert ha["calls"][-1][1]["activity"] == "https://www.netflix.com/title"


def test_proxy_can_be_switched_off(ha, apps_file, monkeypatch):
    monkeypatch.setenv("TV_USE_PROXY_LAUNCHER", "false")
    apps_file.write_text(json.dumps({"a1": "hr.a1.xplore"}), encoding="utf-8")

    tv.tv_open_app("a1")

    assert ha["calls"][-1][1]["activity"] == "hr.a1.xplore"


def test_proxy_left_in_the_foreground_is_a_failure_not_a_launch(ha, apps_file, monkeypatch):
    """The launcher stays on screen with its reason when it cannot resolve the
    package — that must never read as the app having opened."""
    apps_file.write_text(json.dumps({"a1": "hr.a1.xplore"}), encoding="utf-8")
    monkeypatch.setattr(tv, "_wait_for_app_change",
                        lambda before, expected="", timeout=None: "hr.jarvis.launcher")

    result = tv.tv_open_app("a1")

    assert "error" in result
    assert "nije uspio pokrenuti" in result["error"]
    assert "success" not in result


# --- warm-up after launching an app ---------------------------------------

def test_channel_waits_for_a_freshly_launched_app(ha, channels_file, apps_file, monkeypatch):
    """Two seconds after A1 Xplore started, its landing page was still drawing
    and the navigation keys were lost. The tool now sits out the warm-up."""
    slept = []
    monkeypatch.setattr(tv.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("TV_APP_WARMUP_SECONDS", "12")
    apps_file.write_text(json.dumps({"a1": "hr.a1.xplore"}), encoding="utf-8")
    channels_file.write_text(json.dumps({"HRT 1": {"number": 1, "app": ""}}), encoding="utf-8")

    tv.tv_open_app("a1")            # records the launch moment
    result = tv.tv_channel("HRT 1", from_app_home=True)

    assert any(s > 10 for s in slept), f"nije čekao zagrijavanje: {slept}"
    assert "čekao" in result["detail"]


def test_no_warm_up_wait_when_nothing_was_just_launched(ha, channels_file, monkeypatch):
    slept = []
    monkeypatch.setattr(tv.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(tv, "_last_launch_at", 0.0)
    channels_file.write_text(json.dumps({"HRT 1": {"number": 1, "app": ""}}), encoding="utf-8")

    result = tv.tv_channel("HRT 1", from_app_home=True)

    assert all(s <= 5 for s in slept), f"nepotrebno dugo čekanje: {slept}"
    assert "čekao" not in result["detail"]


def test_warm_up_applies_even_without_the_live_tv_step(ha, channels_file, apps_file, monkeypatch):
    """If the app is configured to start straight on live TV, the navigation is
    unnecessary but the wait for it to finish drawing is not."""
    slept = []
    monkeypatch.setattr(tv.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("TV_APP_WARMUP_SECONDS", "12")
    apps_file.write_text(json.dumps({"a1": "hr.a1.xplore"}), encoding="utf-8")
    channels_file.write_text(json.dumps({"HRT 3": {"number": 7, "app": ""}}), encoding="utf-8")

    tv.tv_open_app("a1")
    result = tv.tv_channel("HRT 3", from_app_home=False)

    assert any(s > 10 for s in slept), f"nije čekao zagrijavanje: {slept}"
    assert result["iz_pocetne_stranice"] is False
    sequences = [p["command"] for path, p in ha["calls"] if "send_command" in path]
    assert sequences == [["7", "DPAD_CENTER"]]
