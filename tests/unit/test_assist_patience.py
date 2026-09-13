"""Tests for the patch that stops Assist cutting people off mid-sentence.

The module reaches into Home Assistant's internals, so the part worth testing is
that it locates the right field and refuses to guess when the shape is not what
it expects. Home Assistant itself is not installed here, and does not need to
be: the two classes are recreated with the exact field order they have upstream.
"""

import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "home_assistant"
    / "custom_components"
    / "jarvis"
    / "assist_patience.py"
)
_spec = importlib.util.spec_from_file_location("assist_patience", _MODULE_PATH)
assist_patience = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assist_patience)

set_keyword_default = assist_patience.set_keyword_default


@dataclass(frozen=True)
class AudioSettingsLike:
    """Home Assistant's AudioSettings, same fields in the same order."""

    noise_suppression_level: int = 0
    auto_gain_dbfs: int = 0
    volume_multiplier: float = 1.0
    is_vad_enabled: bool = True
    silence_seconds: float = 0.7


@dataclass
class SegmenterLike:
    """Home Assistant's VoiceCommandSegmenter, same fields in the same order."""

    speech_seconds: float = 0.3
    command_seconds: float = 1.0
    silence_seconds: float = 0.7
    timeout_seconds: float = 15.0
    reset_seconds: float = 1.0


@dataclass
class PartlyRequired:
    """A class whose leading field has no default, to check the offset maths."""

    name: str
    silence_seconds: float = 0.7


@pytest.fixture(autouse=True)
def _restore_defaults():
    """Keep one test's patch from leaking into the next."""
    originals = {
        cls: cls.__init__.__defaults__
        for cls in (AudioSettingsLike, SegmenterLike, PartlyRequired)
    }
    yield
    for cls, defaults in originals.items():
        cls.__init__.__defaults__ = defaults


def test_the_last_field_is_the_one_that_changes():
    previous = set_keyword_default(AudioSettingsLike, "silence_seconds", 3.0)

    assert previous == 0.7
    assert AudioSettingsLike().silence_seconds == 3.0


def test_the_other_fields_are_left_alone():
    """Off-by-one in the defaults offset would quietly corrupt a neighbour."""
    set_keyword_default(AudioSettingsLike, "silence_seconds", 3.0)

    settings = AudioSettingsLike()
    assert settings.noise_suppression_level == 0
    assert settings.auto_gain_dbfs == 0
    assert settings.volume_multiplier == 1.0
    assert settings.is_vad_enabled is True


def test_a_field_in_the_middle_is_found():
    previous = set_keyword_default(SegmenterLike, "timeout_seconds", 30.0)

    assert previous == 15.0
    segmenter = SegmenterLike()
    assert segmenter.timeout_seconds == 30.0
    assert segmenter.silence_seconds == 0.7
    assert segmenter.reset_seconds == 1.0


def test_leading_required_fields_do_not_shift_the_offset():
    previous = set_keyword_default(PartlyRequired, "silence_seconds", 3.0)

    assert previous == 0.7
    assert PartlyRequired(name="x").silence_seconds == 3.0


def test_an_unknown_field_changes_nothing():
    assert set_keyword_default(AudioSettingsLike, "patience", 3.0) is None
    assert AudioSettingsLike().silence_seconds == 0.7


def test_a_class_without_defaults_changes_nothing():
    @dataclass
    class NoDefaults:
        silence_seconds: float

    assert set_keyword_default(NoDefaults, "silence_seconds", 3.0) is None


def test_a_required_field_is_refused_rather_than_mis_targeted():
    """Asking for a field that has no default of its own must not hit another."""
    assert set_keyword_default(PartlyRequired, "name", "y") is None
    assert PartlyRequired(name="x").silence_seconds == 0.7


def test_the_change_can_be_undone():
    previous = set_keyword_default(AudioSettingsLike, "silence_seconds", 3.0)
    set_keyword_default(AudioSettingsLike, "silence_seconds", previous)

    assert AudioSettingsLike().silence_seconds == 0.7


def test_restore_puts_both_timings_back():
    set_keyword_default(AudioSettingsLike, "silence_seconds", 3.0)
    set_keyword_default(SegmenterLike, "timeout_seconds", 30.0)

    assist_patience.restore_patience(
        {
            "silence": (AudioSettingsLike, "silence_seconds", 0.7),
            "turn": (SegmenterLike, "timeout_seconds", 15.0),
        }
    )

    assert AudioSettingsLike().silence_seconds == 0.7
    assert SegmenterLike().timeout_seconds == 15.0


def test_restore_skips_what_was_never_patched():
    """A failed patch records None; restoring it must not write None as a value."""
    assist_patience.restore_patience(
        {"silence": (AudioSettingsLike, "silence_seconds", None)}
    )

    assert AudioSettingsLike().silence_seconds == 0.7
