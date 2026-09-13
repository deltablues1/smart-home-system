"""Let Assist wait for the end of a sentence instead of cutting people off.

Home Assistant ends the recording after 0.7 s of silence
(``AudioSettings.silence_seconds``, which builds the pipeline's
``VoiceCommandSegmenter``). That is short enough that an ordinary pause
mid-sentence ends the turn. Measured on 2026-08-23 by streaming "Upali svjetlo u
dnevnoj sobi" + a 1 s pause + "i pojačaj grijanje na dvadeset dva stupnja" into
the pipeline: only the first half came back.

Two defaults matter, and raising one without the other just moves the cliff:

* ``AudioSettings.silence_seconds`` -- how long a pause may last before the turn
  is considered finished.
* ``VoiceCommandSegmenter.timeout_seconds`` -- the hard cap on one turn,
  15 s by default. Allowing long pauses invites longer sentences, which would
  then hit this instead.

Home Assistant exposes no way to change either for the companion app. The
websocket ``assist_pipeline/run`` command accepts ``noise_suppression_level``,
``auto_gain_dbfs``, ``volume_multiplier`` and ``no_vad`` -- but not
``silence_seconds`` -- and the pipeline record has no such field. Only assist
*satellites* get a VAD-sensitivity entity, and a phone is not one.

So the defaults themselves are moved. This reaches into another integration's
internals, which is worth being nervous about, hence:

* nothing is assumed about the shape -- each field is located by name in the
  signature and skipped if it is missing or has no default of its own,
* every change is verified by constructing the class and reading the value back,
* the previous values are returned so unloading Jarvis puts them back,
* anything unexpected logs a warning and leaves Home Assistant alone, so the
  worst case is the old short timeout rather than a broken install.

The cost is honest: a turn now takes the full pause longer to be answered,
because Assist cannot know a sentence is finished until the silence has actually
lasted that long.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def set_keyword_default(cls: type, field: str, value: Any) -> Any:
    """Replace one keyword default of a class's ``__init__``.

    Returns the previous default, or ``None`` if the field could not be found
    with a default of its own -- in which case nothing was changed.
    """
    init = cls.__init__
    defaults = list(init.__defaults__ or ())
    if not defaults:
        return None

    parameters = list(inspect.signature(init).parameters.values())[1:]
    names = [p.name for p in parameters]
    if field not in names:
        return None

    # Defaults line up with the *last* parameters of the signature.
    index = names.index(field) - (len(parameters) - len(defaults))
    if index < 0:
        return None  # the field is required positionally; nothing to override

    previous = defaults[index]
    defaults[index] = value
    init.__defaults__ = tuple(defaults)
    return previous


def _retune(cls: type, field: str, seconds: float, label: str) -> float | None:
    """Set one timing default and prove it took, or change nothing."""
    previous = set_keyword_default(cls, field, seconds)
    if previous is None:
        _LOGGER.warning(
            "%s.%s no longer has a default; leaving %s as Home Assistant set it",
            cls.__name__, field, label,
        )
        return None

    applied = getattr(cls(), field)
    if applied != seconds:
        _LOGGER.warning(
            "Tried to set %s to %.1fs but it reads back as %.1fs; leaving it alone",
            label, seconds, applied,
        )
        set_keyword_default(cls, field, previous)
        return None

    _LOGGER.info("%s is now %.1fs (was %.1fs)", label, seconds, previous)
    return previous


def apply_patience(silence_seconds: float, max_turn_seconds: float) -> dict[str, Any]:
    """Give Assist room to breathe. Returns what to pass to :func:`restore`."""
    try:
        from homeassistant.components.assist_pipeline.pipeline import AudioSettings
        from homeassistant.components.assist_pipeline.vad import VoiceCommandSegmenter
    except ImportError:
        _LOGGER.warning("assist_pipeline not available; end-of-speech timing untouched")
        return {}

    return {
        "silence": (
            AudioSettings,
            "silence_seconds",
            _retune(AudioSettings, "silence_seconds", silence_seconds, "pause before a turn ends"),
        ),
        "turn": (
            VoiceCommandSegmenter,
            "timeout_seconds",
            _retune(VoiceCommandSegmenter, "timeout_seconds", max_turn_seconds, "maximum turn length"),
        ),
    }


def restore_patience(previous: dict[str, Any]) -> None:
    """Undo :func:`apply_patience` when Jarvis is unloaded."""
    for cls, field, value in previous.values():
        if value is not None:
            set_keyword_default(cls, field, value)
            _LOGGER.info("%s.%s restored to %.1fs", cls.__name__, field, value)
