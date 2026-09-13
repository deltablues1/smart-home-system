"""
Base Interface for Google Workspace ADK System

Abstract base class that defines the interface contract for different
communication channels (CLI, Telegram, Web API, etc.)
"""

import asyncio
import os
import logging
import re
import time
import uuid
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from services.voice_fast_path import execute_fast_smart_home_command
load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnContext:
    """Everything one request needs, carried instead of shared.

    `system.session_id`, `system.user_id` and `system.orchestrator_helper`
    were one set of attributes serving every caller of a single system
    object. Two conversations rebound them under each other, and a deferred
    voice task was worse than that: `ensure_future` queues the coroutine, and
    the attribute is read when its body finally runs — by which time the
    runner belonged to a later turn, in a different session, possibly for a
    different user.

    Passing the context makes that impossible instead of unlikely. Nothing
    here is read off `self`.
    """

    session_id: str
    user_id: str
    helper: Any


SMART_HOME_KEYWORDS = {
    "svjetlo", "upal", "ugas", "ukljuc", "uključi", "iskljuc", "isključi",
    "utič", "utic", "bojler", "fotelja", "terasa", "boravak", "hodnik",
    "kuhinja", "kupaona", "soba", "film", "nocno", "noćno", "dolazak",
    "odlazak", "pametna kuća", "pametna kuca", "smart home", "scene", "scena",
    # TV / media (preko Home Assistanta). Golo "tv" se NE stavlja ovdje jer
    # substring match hvata "tvoj"/"molitva" — rješava ga _TV_WORD_RE.
    "televizor", "youtube", "jutjub", "netflix", "pojačaj", "pojacaj",
    "stišaj", "stisaj", "glasnoć", "glasnoc", "kanal", "pauziraj",
    "aplikacij", "xplore", "program", "hrt", "arena sport", "sportklub",
    # Shopping list. It lives on the smart_home agent (the list is an HA todo
    # entity), but nothing above matches "stavi ulje na listu" or "kupio sam
    # sve osim mlijeka", so the voice lane would have handed those to voice_qa,
    # which has no tools and would have answered as if it had done something.
    # "shopping lista" is what the user actually says -- the HA entity is
    # called Shopping List, so the English word is the natural one. Without it
    # the phrase fell through to voice_qa and only reached the tools via the
    # escalation hop. "shoping" is the spelling STT keeps producing.
    "shopping", "shoping",
    # "kupovna lista" / "kupovnu listu" -- another way to say it, and the
    # one that was actually spoken next.
    "kupovn",
    "listu za kupovinu", "lista za kupovinu", "liste za kupovinu",
    "na listu", "s liste", "na popis", "s popisa", "popis za kupovinu",
    "za kupovinu", "kupio sam", "kupila sam", "kupili smo", "za ducan",
    "za trgovinu", "sto trebam kupiti", "sta trebam kupiti",
    # Everything Home Assistant knows is reached through the same agent:
    # "ima li grešaka u home assistantu", "što javlja senzor", "potrošnja".
    "home assistant", "senzor", "potrošnj", "potrosnj", "potrošil", "potrosil",
}

# Word-boundary match for the bare word "tv" ("upali tv", "tv u dnevnoj").
_TV_WORD_RE = re.compile(r"\btv\b")


def turn_busy_notice_after_seconds() -> float:
    """How long a second message waits before being told the session is busy."""
    try:
        return max(1.0, float(os.getenv("TURN_BUSY_NOTICE_AFTER_SECONDS", "10")))
    except ValueError:
        return 10.0


def _fold_text(text: str) -> str:
    """Lowercase and strip diacritics, so "poštuj" and "postuj" match alike."""
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


# Philosophy routing, WHOLE WORDS only. The old set held bare stems "etik"
# and "logik" matched as substrings, so "napravi etiketu" and "provjeri
# logiku" both scored as philosophy — and the caller flipped the whole
# process into CLASSROOM on them. Same lesson as _TV_WORD_RE, plus one step
# further: "logika" is gone entirely. Even as a whole word it is ordinary
# Croatian ("logika rasporeda", "logika koda"), and no boundary separates
# that from the discipline.
_PHILOSOPHY_WORD_RE = re.compile(
    r"\b("
    r"sokrat\w*|socrates|platon\w*|plato|aristotel\w*|aristotle|"
    r"kant|kantov\w*|nietzsche|niceov\w*|hegel\w*|descartes|dekart\w*|"
    r"filozof\w*|philosoph\w*|metafizi\w*|epistemolog\w*|ontolog\w*|"
    r"etika|etike|etici|etiku|etikom|etick\w*|"
    r"stoicizm\w*|stoick\w*|epikur\w*|seneka|seneca|"
    r"marko aurelije|marcus aurelius"
    r")\b"
)


def looks_philosophical(text: str) -> bool:
    """Does this message ask for the philosophy lane?

    Routing only — it picks the agent for ONE turn. Entering CLASSROOM for
    the whole process takes an explicit command, because active_mode is
    global: a keyword that flips it from one channel strands every other
    channel in the wrong mode.
    """
    return bool(_PHILOSOPHY_WORD_RE.search(_fold_text(text)))


# Explicit entry/exit for the global Philosophy Classroom. The exit already
# existed (Telegram /leave, CLI "leave classroom"); the entry was a keyword
# side effect, which is exactly what let an ordinary request flip the mode.
# Whole-utterance phrases asking after a deferred job, ASCII-folded.
JOB_STATUS_QUESTIONS = frozenset({
    "je li gotovo", "jel gotovo", "jeli gotovo", "je l gotovo",
    "jesi li zavrsio", "jesi zavrsio", "jesi li gotov", "jesi gotov",
    "ima li novosti", "sto je s onim", "sta je s onim",
    "kako napreduje", "jesi li nasao", "jesi nasao",
})


def _close_background_job(job_id: str, task) -> None:
    """Mark a deferred job finished once its task settles."""
    from services import background_jobs

    try:
        if task.cancelled():
            background_jobs.finish(job_id, failed=True)
            return
        error = task.exception()
        if error is not None:
            background_jobs.finish(job_id, answer=str(error)[:500], failed=True)
            return
        background_jobs.finish(job_id, answer=task.result() or "")
    except Exception:
        logger.warning("Could not close background job %s", job_id, exc_info=True)


CLASSROOM_ENTER_COMMANDS = frozenset({
    "/classroom", "classroom", "philosophy classroom",
    "udji u ucionicu", "filozofska ucionica",
})
CLASSROOM_LEAVE_COMMANDS = frozenset({
    "/leave", "leave classroom", "izadi iz ucionice", "napusti ucionicu",
})

CHRISTIAN_KEYWORDS = {
    "krsc", "kršć", "biblij", "katekiz", "molitv", "duhovn", "augustin",
    "ignacije", "razluc", "razluč", "ispit savjesti", "examen", "egzamen",
    "papa", "enciklik", "vatikan", "crkv", "kempis", "isus", "kristov",
}

# Full phrases only — matched against _normalize_voice_text output. Bare
# "vrijeme"/"datum" must NOT go here: substring match would hijack weather
# questions ("kakvo je vrijeme sutra") and knowledge questions ("koji je
# datum rođenja..."). _is_time_or_date_request additionally requires the
# phrase to end the utterance (modulo "danas"/"sada").
TIME_DATE_KEYWORDS = {
    "koliko je sati", "koji je datum", "koji je danas datum", "koji datum",
    "danasnji datum", "koji je dan", "koji dan", "koliko je ura", "koliko sati",
}

# Trailing words still compatible with a local time/date answer.
TIME_DATE_ALLOWED_TAILS = {"", "danas", "sada", "sad", "trenutno"}

# Weather intent — checked before the time/date branch would ever see the
# word "vrijeme". Safe long stems as substrings (ASCII-folded, matched
# against _normalize_voice_text output)...
WEATHER_VOICE_KEYWORDS = {
    "prognoz", "vremensk", "temperatur", "stupnjev", "oblacno", "suncano",
    "grmljavin", "pljusak", "pljusk", "nevrijeme", "snijezi", "snjezi",
}

# ...and short weather words with word boundaries, because bare substrings
# would false-positive ("kisi" in "kisik", same lesson as _TV_WORD_RE).
_WEATHER_WORD_RE = re.compile(
    r"\b(kis[aeiu]|snijeg[au]?|snjezn\w*|vjetar|vjetr[au]|vjetrovit\w*|"
    r"magl[aeiu]|maglovit\w*)\b"
)

# "vrijeme" counts as weather only next to a weather cue, not on its own
# ("koliko je vremena" / "imamo li vrijeme za sastanak" must not match).
_WEATHER_VRIJEME_CUES = (
    "kakvo", "kakva", "kako je vani", "sutra", "danas", "vani",
    "prekosutra", "za vikend",
)

# Full phrases only (ASCII-folded) — the daily briefing lane. Deliberately
# specific: "danas" alone must not hijack ordinary questions.
BRIEFING_VOICE_KEYWORDS = (
    "dnevni pregled", "dnevni izvjestaj", "jutarnji pregled", "jutarnji brifing",
    "dnevni brifing", "sto me ceka danas", "sto me danas ceka",
    "sta me ceka danas", "daily briefing",
)

HR_WEEKDAY_NAMES = (
    "ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja",
)

# A device NOUN is not a device COMMAND. "bojler", "terasa", "kuhinja" and
# "kanal" are all ordinary Croatian as well as things in this house, so
# SMART_HOME_KEYWORDS matches questions that merely mention one. Measured
# 2026-09-06 on the Pi: "istrazi prednosti toplinskih pumpi u odnosu na plinski
# bojler" was routed to smart_home, which answered "to je izvan mojih
# mogucnosti" in two seconds. Nobody was asking about the boiler in the
# bathroom.
#
# These stems mark a request as research or comparison, and send it to the
# orchestrator instead of the device lane. Kept deliberately tight: the cost of
# over-routing here is a slower answer, while the cost of under-routing is a
# flat refusal, so only unambiguous research verbs belong. Matched against
# _normalize_voice_text output, so ASCII-folded stems only.
RESEARCH_VOICE_KEYWORDS = {
    "istraz", "usporedi", "usporedb", "isplati li se", "prednosti",
    "nedostat", "razlika izmedu", "sto je bolje", "sta je bolje",
    "preporuci", "koliko kosta", "koliko stoji",
}

# "Kolika je temperatura u kupaoni?" is a question for the sensor on the
# bathroom wall, but "temperatur" is also a weather keyword, and the weather
# lane is checked before the device lanes -- so it answered with the Zagreb
# forecast. Measured 2026-09-13 on the Pi. A measurement word next to a place in
# the house, a consumption word, or a question about the past sends the request
# to the house's own sensors instead. Matched against _normalize_voice_text
# output, so ASCII-folded stems only.
HOME_SENSOR_MEASURE_STEMS = (
    "temperatur", "stupnj", "toplo", "hladno", "vlag", "tlak", "zrak", "cestic",
    "prasin", "potrosnj", "potrosil", "potrosen", "struj", "snag", "napon",
    "kilovat", "kwh",
)
HOME_SENSOR_CONSUMPTION_STEMS = (
    "potrosnj", "potrosil", "potrosen", "struj", "snag", "napon", "kilovat", "kwh",
)
# Whole words: "osoba" contains "soba".
_HOME_SENSOR_PLACE_RE = re.compile(
    r"\b(kupaon\w*|sob[aeiu]|sobom|boravk\w*|dnevn\w*|kuhinj\w*|hodnik\w*|"
    r"ulaz\w*|blag[ao]vaon\w*|teras\w*|kat[au]?|prizemlj\w*|kuci|doma|unutra|"
    r"senzor\w*)\b"
)
HOME_SENSOR_HISTORY_CUES = (
    "bila", "bilo", "bio ", "jucer", "prosjek", "najvis", "najniz", "izmjer",
    "zabiljez", "ovaj tjedan", "prosli tjedan", "ovaj mjesec",
)
# Future and forecast wording keeps a question on the weather lane.
HOME_SENSOR_FORECAST_CUES = (
    "prognoz", "sutra", "prekosutra", "vikend", "bit ce", "hoce li", "ce biti",
)


# "Sutra ujutro u 7 upali TV i pusti neku pjesmu" is a SCHEDULING request
# wearing a device command. The device lane saw "upali" and "TV", took it,
# and smart_home answered honestly that it cannot defer anything -- measured
# 2026-09-12, on something that used to work.
#
# Two halves, and both are required. A future marker alone would catch
# "kolika je bila temperatura ujutro", which is a sensor question the house
# agent should keep. An action stem alone is just an ordinary command.
SCHEDULE_TIME_MARKERS = {
    "sutra", "prekosutra", "poslije sutra",
    "svaki dan", "svako jutro", "svaku vecer", "svake veceri", "svaku noc",
    "radnim danima", "vikendom", "svaki tjedan", "svaki mjesec",
    "svaki ponedjeljak", "svaki utorak", "svaku srijedu", "svaki cetvrtak",
    "svaki petak", "svaku subotu", "svaku nedjelju",
    "za sat vremena", "za pola sata", "za dva sata", "kasnije",
}

# "za 10 minuta", "u 7", "u 21h", "u 7:30" -- a clock time or a delay.
_SCHEDULE_CLOCK_RE = re.compile(
    r"\bza \d{1,3}\s*(min|sat|h)|\bu \d{1,2}([:.]\d{2})?\s*(h|sati|sat)?\b"
)

# What the scheduled turn would actually do. Without one of these a time
# expression is just a time.
SCHEDULE_ACTION_STEMS = {
    "upal", "ugas", "ukljuc", "iskljuc", "pojacaj", "stisaj", "pusti",
    "posalji", "napravi", "javi", "probudi", "podsjeti", "prebaci",
    "otvori", "zatvori", "skuhaj", "zapocni", "pokreni", "provjeri",
}


BUSINESS_ORCHESTRATOR_KEYWORDS = {
    "mail", "email", "gmail", "kalendar", "calendar", "drive", "docs",
    "dokument", "dokumenti", "sheet", "sheets", "tablica", "tablice",
    "zadatak", "zadaci", "contacts", "kontakt", "kontakti",
    # Action tasks that need tools -> must hit the orchestrator upfront.
    # Matched against _normalize_voice_text output, so use ASCII-folded stems.
    "faktura", "ponud", "podsjetnik", "podsjeti", "sastanak",
    "posalji", "rezervi", "zakazi", "racun",
}

# Short confirmation/abort replies that must stay in a pinned confirmation lane
# ("da" after "Predlažem utorak u 10. Potvrđuješ?"). ASCII-folded forms.
VOICE_CONFIRMATION_WORDS = {
    "da", "ne", "moze", "potvrdujem", "potvrdi", "u redu", "ok", "okej",
    "tako je", "tocno", "odustani", "stani", "nemoj",
}

# How long after a pinned-lane turn short follow-ups keep routing to it.
VOICE_LANE_PIN_TTL_SECONDS = 120

GENERAL_VOICE_PREFIXES = (
    "sto ",
    "što ",
    "tko ",
    "ko je ",
    "objasni",
    "reci mi",
    "reci nesto",
    "reci nešto",
    "kako ",
    "zasto ",
    "zašto ",
)

VOICE_ROUTING_USER_PREFIXES = (
    "rpi-voice",
    "live-voice",
    "telegram-voice",
    # Home Assistant Assist (the phone, the wall panel) is a spoken channel like
    # the others and gets the same lanes. Without this every question asked
    # through Assist went to the 16-tool orchestrator: slow enough that the
    # answer often arrived after the user had given up, ~7k tokens instead of
    # ~1.5k, no deterministic smart-home fast path, and — measured 2026-09-02 —
    # a single philosophy question flipped the whole process into CLASSROOM
    # mode, where every later turn on every channel went to Socrates.
    "ha-assist",
)

# Channels whose replies get READ ALOUD; used only to choose the failure
# wording, because Home Assistant Assist speaks whatever comes back and it
# spent a day reading raw "litellm.BadRequestError: AnthropicException - {...}"
# JSON out loud. Same members as the routing tuple today, kept separate because
# the two answer different questions: how to route, and whether anyone hears it.
SPOKEN_CHANNEL_USER_PREFIXES = VOICE_ROUTING_USER_PREFIXES

# The one channel whose window closes on its own: a phone's Assist dialogue
# ends when the screen sleeps, taking the pipeline run with it.
HA_ASSIST_USER_PREFIX = "ha-assist"

LOCAL_VOICE_ROUTE = "local_voice_response"
WEATHER_VOICE_ROUTE = "local_weather_response"
BRIEFING_VOICE_ROUTE = "local_briefing_response"
ORCHESTRATOR_VOICE_ROUTE = "orchestrator"
# voice_qa (tool-less) emits this sentinel when the request is actually a task;
# the interface then re-routes the original message to the orchestrator.
ESCALATE_SENTINEL = "[[ESCALATE]]"
# Smart-home matching (room/scene aliases, response modes) lives in
# services/voice_fast_path.py — the copy that used to sit here drifted from it.


class BaseInterface(ABC):
    """
    Abstract base class for all interface implementations.

    All interfaces share the same WorkspaceADKSystem but differ in how they:
    - Receive user input
    - Send responses
    - Handle sessions
    - Format output
    """

    # How many session runners to keep. Building one is cheap; a long-lived
    # web process just must not keep every session it has ever seen.
    _MAX_CACHED_HELPERS = 32

    def __init__(self, session_prefix: str = "interface"):
        """
        Initialize base interface.

        Args:
            session_prefix: Prefix for session IDs (e.g., 'cli', 'telegram', 'web')
        """
        self.session_prefix = session_prefix
        self.system = None
        self.active_sessions: Dict[str, Any] = {}
        # session_id -> (agent_name, expires_at): keeps short confirmation
        # replies in the same direct voice lane (write confirmations).
        self._voice_pinned_lane: Dict[str, tuple] = {}

        # session_id -> RunnerHelper. A cache, not a "current" runner: two
        # sessions can be mid-turn at once, and neither may overwrite the
        # other's. Insertion-ordered, so the oldest entry is evicted first.
        self._runner_helpers: Dict[str, Any] = {}

        # session_id -> what this session is busy with right now. A second
        # message on a busy session gets told, instead of queueing behind a
        # lock until the client gives up: measured 2026-09-06, "Je li gotovo?"
        # sent during a 15-minute research run waited 4.5 minutes and returned
        # nothing at all. On voice, silence is indistinguishable from a crash.
        self._inflight_turns: Dict[str, Dict[str, Any]] = {}

        # Last session/user seen. Display only — nothing dispatches on these.
        self.last_session_id: Optional[str] = None
        self.last_user_id: Optional[str] = None

        # Lazy import to avoid circular dependencies
        self._system_class = None

    def _reclaim_orphaned_jobs(self) -> None:
        """Release sessions claimed by jobs whose process is gone.

        Without this, one crash during a long research task would claim that
        conversation forever: every later orchestrator turn in it would be
        told to wait for a job that no longer exists.
        """
        try:
            from services import background_jobs
            # Name this process's notepad before touching it. Sharing one file
            # meant any interface starting up could declare another's live job
            # interrupted and release a session still being written to.
            background_jobs.set_owner(self.session_prefix)
            background_jobs.mark_orphans_interrupted()
        except Exception:
            logger.debug("Could not reclaim orphaned jobs", exc_info=True)

    def _get_system_class(self):
        """Lazy load WorkspaceADKSystem to avoid import issues."""
        if self._system_class is None:
            # Import here to avoid circular imports
            from main import WorkspaceADKSystem
            self._system_class = WorkspaceADKSystem
        return self._system_class

    def generate_session_id(self, user_id: str) -> str:
        """
        Generate a unique session ID for a user.

        Args:
            user_id: Unique identifier for the user

        Returns:
            Session ID string
        """
        return f"{self.session_prefix}-{user_id}-{uuid.uuid4().hex[:8]}"

    def initialize_system(self) -> None:
        """
        Initialize the WorkspaceADKSystem.

        This creates and configures the agent system.
        """
        logger.info(f"Initializing system for {self.session_prefix} interface...")

        SystemClass = self._get_system_class()
        self.system = SystemClass()
        self.system.initialize_agents()
        self._reclaim_orphaned_jobs()

        logger.info(f"System initialized for {self.session_prefix} interface")

    def _get_worker_agent(self, agent_name: str):
        """Return a loaded worker agent by name, if present."""
        if not self.system:
            return None
        for agent in self.system.worker_agents:
            if getattr(agent, "name", "") == agent_name:
                return agent
        return None

    @staticmethod
    def _normalize_voice_text(text: str) -> str:
        return _fold_text(text)

    def _should_use_voice_direct_routing(self, user_id: str) -> bool:
        """Enable direct routing only for voice-tagged users when the feature is on.

        The flag comes from the deployment profile (rpi-home defaults to
        True; VOICE_DIRECT_ROUTING env still overrides) — reading the env
        directly here once silently disabled direct routing on a Pi whose
        .env lacked the variable.
        """
        try:
            from config.deployment_config import is_voice_direct_routing
            direct_enabled = is_voice_direct_routing()
        except Exception:
            direct_enabled = os.getenv("VOICE_DIRECT_ROUTING", "false").lower() in (
                "1", "true", "yes", "on"
            )
        if not direct_enabled:
            return False
        return user_id.startswith(VOICE_ROUTING_USER_PREFIXES)

    def _match_direct_voice_agent(self, message: str) -> Optional[str]:
        """Pick direct voice target before orchestrator fallback."""
        msg_lower = message.lower()
        if looks_philosophical(message):
            return "socrates"
        if any(kw in msg_lower for kw in SMART_HOME_KEYWORDS) or _TV_WORD_RE.search(msg_lower):
            return "smart_home"
        if any(kw in msg_lower for kw in CHRISTIAN_KEYWORDS):
            return "christian_guide"
        if self._is_time_or_date_request(message):
            return "secretary"
        if msg_lower.startswith(GENERAL_VOICE_PREFIXES):
            return "voice_qa"
        return None

    def _looks_like_business_orchestrator_task(self, message: str) -> bool:
        msg_lower = self._normalize_voice_text(message)
        return any(kw in msg_lower for kw in BUSINESS_ORCHESTRATOR_KEYWORDS)

    def _looks_like_scheduled_request(self, message: str) -> bool:
        """A command aimed at a moment that has not arrived yet."""
        msg = self._normalize_voice_text(message)
        timed = any(kw in msg for kw in SCHEDULE_TIME_MARKERS) or bool(
            _SCHEDULE_CLOCK_RE.search(msg)
        )
        if not timed:
            return False
        return any(stem in msg for stem in SCHEDULE_ACTION_STEMS)

    def _looks_like_research_request(self, message: str) -> bool:
        """A question ABOUT something, not a command to a device."""
        msg_lower = self._normalize_voice_text(message)
        return any(kw in msg_lower for kw in RESEARCH_VOICE_KEYWORDS)

    async def _speak_working_ack(self) -> None:
        """Speak a short 'working on it' cue before a long orchestrator run.

        The wake-word runner registers `voice_ack_hook` (a blocking
        text->speech function); other interfaces leave it unset, so this is a
        no-op for web/Telegram. Runs in an executor so TTS playback doesn't
        block the event loop.
        """
        hook = getattr(self, "voice_ack_hook", None)
        if not callable(hook):
            return
        text = os.getenv(
            "VOICE_WORKING_ACK_TEXT",
            "Radim na tome. Ovo može potrajati minutu ili dvije.",
        ).strip()
        if not text:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(None, hook, text)
        except Exception:
            logger.debug("Working-ack hook failed", exc_info=True)

    @staticmethod
    def _plan_execute_applies(message: str) -> bool:
        """Would this request be decomposed instead of run in one pass?

        Streaming has to ask before it starts: plan-execute runs each step in
        its own isolated session and produces no token stream to forward, so
        the two cannot be mixed. Asking here is what stops the same request
        behaving differently in the browser and everywhere else.
        """
        if os.getenv("USE_PLAN_EXECUTE", "false").lower() != "true":
            return False
        try:
            from agents.adk_agents.plan_execute import _looks_multi_step
            return _looks_multi_step(message)
        except Exception:
            logger.debug("Plan-execute prefilter failed", exc_info=True)
            return False

    @staticmethod
    def _is_job_status_question(message: str) -> bool:
        """Is the user asking about the long task, rather than asking for one?

        Deliberately a small phrase list, matched against the whole utterance
        like the time/date lane: a loose match here would swallow ordinary
        questions and answer them with a status report.
        """
        text = _fold_text(message).strip(" ?!.,")
        return text in JOB_STATUS_QUESTIONS

    def _answer_job_status(self, session_id: str) -> Optional[str]:
        """What the last long job for this conversation is doing, if any."""
        from services import background_jobs

        job = background_jobs.latest_for_session(session_id)
        if job is None:
            return None
        return background_jobs.describe(job)

    @staticmethod
    def session_busy_answer(session_id: str) -> Optional[str]:
        """What to say instead of starting a second run in this conversation.

        A deferred job keeps writing to its ADK session long after the turn
        that started it returned, and the session lock went with that turn.
        A second orchestrator run in the same session interleaves two
        conversations into one transcript.

        The check has to sit on every orchestrator entry, not just the voice
        one that creates these jobs: opening the same conversation in the
        browser while a voice job runs reaches the same session.
        """
        try:
            from services import background_jobs
            holding = background_jobs.active_for_session(session_id)
        except Exception:
            logger.debug("Could not check for a running job", exc_info=True)
            return None
        if holding is None:
            return None
        logger.info(
            "Session '%s' is held by job %s; not starting a second run",
            session_id, holding.get("job_id"),
        )
        from services import background_jobs as jobs
        return jobs.describe(holding)

    def _orchestrate(self, ctx: "TurnContext"):
        """A callable that runs the orchestrator for THIS request.

        Binding the context here means a caller cannot accidentally reach for
        the shared runner, including a caller that only executes later. It is
        also the one place every orchestrator run passes through, which is
        why the busy check lives here rather than at each call site.
        """
        async def run(prompt: str) -> str:
            busy = self.session_busy_answer(ctx.session_id)
            if busy is not None:
                return busy
            return await self.system.run_orchestration(
                prompt, helper=ctx.helper, user_id=ctx.user_id
            )

        return run

    async def _run_orchestration_for_voice(
        self, prompt: str, user_id: str, question: str, ctx: "TurnContext"
    ) -> str:
        """Run the orchestrator without betting the answer on the window staying open.

        Home Assistant Assist on a phone gives up long before the orchestrator
        does. Measured 2026-09-03: a 53 s turn was spoken back in full, while a
        3 min 25 s research task finished on this side with nobody left
        listening. So the run gets a grace period; past it the user is told the
        work continues, and `services/late_answer` puts the result on the phone
        when it lands.

        Only Assist defers. The wake word has a speaker in the room and a loop
        patient enough to use it, and text channels have no window to lose.

        The runner comes from `ctx`, and this is the one place where that is
        not merely tidier. The deferred task runs its body minutes after it was
        queued; reading `self.system.orchestrator_helper` inside it would pick
        up whichever runner the NEXT turn installed, so a long research answer
        could land in someone else's session.
        """
        run = self._orchestrate(ctx)

        # `run` refuses on its own when a job still holds this conversation,
        # so a busy session answers here without ever starting a task.
        return await self._defer_to_phone_if_slow(
            lambda: run(prompt), user_id=user_id, question=question, ctx=ctx
        )

    async def _defer_to_phone_if_slow(
        self, start, *, user_id: str, question: str, ctx: "TurnContext"
    ) -> str:
        """Await the work; past the grace period, promise it to the phone.

        Extracted so the DIRECT voice lanes get the same net. They were
        assumed fast because they skip the orchestrator, but smart_home can
        sit for half a minute: measured 2026-09-06, "pusti Arena Sport 1"
        took 40s, 23.6s of it waiting for the A1 Xplore app to load. Assist
        on the phone stopped listening long before that, and because no job
        was ever created there was no notification either -- the request
        succeeded on the TV and the user heard nothing at all.

        `start` is a callable returning the coroutine, not the coroutine
        itself: on the non-Assist path it must never be created if it is
        not awaited.
        """
        from services import background_jobs

        if not user_id.startswith(HA_ASSIST_USER_PREFIX):
            return await start()

        try:
            grace = float(os.getenv("VOICE_DEFER_AFTER_SECONDS", "25"))
        except ValueError:
            grace = 25.0
        if grace <= 0:
            return await start()

        task = asyncio.ensure_future(start())
        try:
            # shield, not wait_for on the task itself: the timeout must end the
            # waiting, never the work.
            return await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except asyncio.TimeoutError:
            pass

        from services.late_answer import deliver_when_done

        # Written down before the promise is spoken. The work is still an
        # asyncio.Task and still cannot be resumed, but a job dropped by a
        # restart now leaves a record instead of silence.
        job_id = background_jobs.start(ctx.session_id, ctx.user_id, question)
        task.add_done_callback(
            lambda finished: _close_background_job(job_id, finished)
        )

        deliver_when_done(task, question)
        logger.info(
            "Voice task deferred after %.0fs as job %s; answer goes to the phone",
            grace, job_id,
        )
        return os.getenv(
            "VOICE_DEFER_ACK_TEXT",
            "Ovo će potrajati. Nastavljam raditi i poslat ću ti odgovor na mobitel čim bude gotov.",
        )

    @staticmethod
    def _voice_friendly_error(exc: Exception) -> str:
        """Short spoken-Croatian failure message with a rough cause."""
        low = str(exc).lower()
        if "loop guard" in low:
            import re
            m = re.search(r"tool '([^']+)'", str(exc))
            tool = m.group(1) if m else "jedan od alata"
            return (
                f"Nisam uspio dovršiti zadatak — alat {tool} stalno javlja "
                "grešku pa sam odustao. Pokušaj drugačije formulirati zahtjev."
            )
        if "429" in low or "rate limit" in low or "resource_exhausted" in low or "overloaded" in low:
            return (
                "Nisam uspio — servis za umjetnu inteligenciju je trenutno "
                "preopterećen. Pričekaj minutu pa pokušaj ponovno."
            )
        if "timeout" in low or "timed out" in low or "cancelled" in low:
            return "Nisam uspio — zadatak je predugo trajao pa je prekinut."
        if "401" in low or "403" in low or "authentication" in low or "api key" in low or "permission" in low:
            return (
                "Nisam uspio — imam problem s pristupom servisu, izgleda kao "
                "problem s ovlastima ili ključem."
            )
        if "connection" in low or "network" in low or "dns" in low or "unreachable" in low:
            return "Nisam uspio — ne mogu se spojiti na servis. Provjeri internet vezu."
        if "quota" in low:
            return "Nisam uspio — potrošena je kvota prema servisu za danas ili ovu minutu."
        if "credit balance" in low or "billing" in low or "purchase credits" in low:
            return (
                "Nisam uspio — potrošen je kredit na računu za umjetnu "
                "inteligenciju. Treba ga nadoplatiti."
            )
        return (
            "Nisam uspio izvršiti zadatak zbog tehničke greške. "
            "Detalji su zapisani u logu."
        )

    def _is_time_or_date_request(self, message: str) -> bool:
        msg_lower = self._normalize_voice_text(message).strip(" ?!.,")
        for kw in TIME_DATE_KEYWORDS:
            idx = msg_lower.find(kw)
            if idx == -1:
                continue
            # A real time/date question ends with the phrase ("reci mi koji je
            # datum"). A tail means "datum"/"dan" is part of something else
            # ("koji je datum rođenja pape") — not ours to answer locally.
            tail = msg_lower[idx + len(kw):].strip(" ?!.,")
            if tail in TIME_DATE_ALLOWED_TAILS:
                return True
        return False

    def _is_briefing_request(self, message: str) -> bool:
        msg_lower = self._normalize_voice_text(message)
        return any(kw in msg_lower for kw in BRIEFING_VOICE_KEYWORDS)

    def _is_meeting_scheduling_request(self, message: str) -> bool:
        """Pure meeting-scheduling requests go straight to secretary.

        Without this, "zakaži sastanak s Anom" hits the business keywords
        ("sastanak", "zakazi") and lands on the minutes-slow orchestrator, so
        the secretary confirmation lane never engages. Mixed multi-step
        requests ("zakaži sastanak i pošalji mail") still go to the
        orchestrator.
        """
        msg = self._normalize_voice_text(message)
        has_subject = "sastanak" in msg or "sastanka" in msg or "termin" in msg
        has_verb = any(v in msg for v in ("zakazi", "dogovori", "nadi termin", "nadji termin"))
        if not (has_subject and has_verb):
            return False
        multi_step_markers = (
            "posalji mail", "posalji email", "posalji poruku", "mailom",
            "dokument", "istrazi", "izvjestaj",
        )
        return not any(m in msg for m in multi_step_markers)

    def _looks_like_home_sensor_request(self, message: str) -> bool:
        """A measurement question about this house, not about the weather."""
        msg = self._normalize_voice_text(message)
        if not any(stem in msg for stem in HOME_SENSOR_MEASURE_STEMS):
            return False
        if any(cue in msg for cue in HOME_SENSOR_FORECAST_CUES):
            return False
        if _HOME_SENSOR_PLACE_RE.search(msg):
            return True
        if any(stem in msg for stem in HOME_SENSOR_CONSUMPTION_STEMS):
            return True
        return any(cue in msg for cue in HOME_SENSOR_HISTORY_CUES)

    def _is_weather_request(self, message: str) -> bool:
        msg_lower = self._normalize_voice_text(message)
        if any(kw in msg_lower for kw in WEATHER_VOICE_KEYWORDS):
            return True
        if _WEATHER_WORD_RE.search(msg_lower):
            return True
        if "vrijeme" in msg_lower and any(cue in msg_lower for cue in _WEATHER_VRIJEME_CUES):
            return True
        return False

    def _build_time_or_date_response(self, message: str) -> str:
        tz_name = os.getenv("USER_TIMEZONE", "Europe/Zagreb")
        now = datetime.now(ZoneInfo(tz_name))
        msg_lower = self._normalize_voice_text(message)

        if "datum" in msg_lower or "koji je dan" in msg_lower or "koji dan" in msg_lower:
            # strftime %A would speak the English weekday through TTS; the
            # systemd service has no Croatian locale, so map it ourselves.
            day_name = HR_WEEKDAY_NAMES[now.weekday()]
            return f"Danas je {day_name}, {now.day}. {now.month}. {now.year}."

        return now.strftime("Trenutno je %H:%M.")

    async def _build_weather_response(self, message: str) -> str:
        from services.voice_weather import get_voice_weather_report

        report = await get_voice_weather_report(message)
        if report:
            return report
        return (
            "Ne mogu trenutno dohvatiti vremensku prognozu. "
            "Pokušaj ponovno za koju minutu."
        )

    async def _build_briefing_response(self) -> str:
        from config.user_context import get_default_user_context
        from services.daily_briefing import get_daily_briefing

        try:
            return await get_daily_briefing(get_default_user_context(channel="voice"))
        except Exception as e:
            logger.error(f"Daily briefing failed: {e}")
            return "Ne mogu trenutno sastaviti dnevni pregled. Pokušaj ponovno kasnije."

    def _classify_voice_route(self, message: str) -> tuple[str, Optional[str]]:
        """
        Lightweight voice pre-router.

        Returns:
            (route_type, route_target)
            route_type:
              - LOCAL_VOICE_ROUTE
              - WEATHER_VOICE_ROUTE
              - ORCHESTRATOR_VOICE_ROUTE
              - "agent"
        """
        if self._is_time_or_date_request(message):
            return LOCAL_VOICE_ROUTE, None

        # Briefing phrases are specific ("dnevni pregled") — check before the
        # business keywords so "što me čeka danas" doesn't hit the orchestrator.
        if self._is_briefing_request(message):
            return BRIEFING_VOICE_ROUTE, None

        # Pure meeting scheduling → secretary lane (with its confirmation
        # pin), before the business keywords would send it to the orchestrator.
        if self._is_meeting_scheduling_request(message):
            return "agent", "secretary"

        if self._looks_like_business_orchestrator_task(message):
            return ORCHESTRATOR_VOICE_ROUTE, None

        # Scheduling BEFORE every agent lane: a deferred command still names
        # a device, and the device lane would take it and then explain that it
        # cannot defer. Only the orchestrator reaches the scheduler agent.
        if self._looks_like_scheduled_request(message):
            return ORCHESTRATOR_VOICE_ROUTE, None

        # Research/comparison AFTER the business check (so "posalji mail s
        # usporedbom" still goes the business way) but BEFORE every agent lane:
        # a question that merely names a device is not a command for it.
        if self._looks_like_research_request(message):
            return ORCHESTRATOR_VOICE_ROUTE, None

        # The house's own sensors BEFORE weather: "temperatura u kupaoni" also
        # contains a weather keyword, and the weather lane used to take it.
        if self._looks_like_home_sensor_request(message):
            return "agent", "smart_home"

        # Weather AFTER the business check so mixed requests ("pošalji mail s
        # prognozom") still reach the orchestrator, but before every agent
        # lane: no agent has a weather tool, this is the only reliable path.
        if self._is_weather_request(message):
            return WEATHER_VOICE_ROUTE, None

        direct_agent = self._match_direct_voice_agent(message)
        if direct_agent in {"socrates", "smart_home", "christian_guide", "secretary"}:
            return "agent", direct_agent

        if direct_agent == "voice_qa":
            return "agent", "voice_qa"

        # Default for anything not clearly a tool/business task: the fast,
        # tool-less voice_qa agent (Claude Sonnet) instead of the heavy 14-tool
        # orchestrator. Real tasks are caught upstream by the business-keyword
        # check; anything that slips through is handled by voice_qa's
        # escalate-to-orchestrator handoff (Phase 2).
        return "agent", "voice_qa"

    # Explicit confirmations that arm a pending protected action. Matched
    # against the normalized message: exact, or as the leading word ("da,
    # ugasi ga"). Anything else cancels — an unrelated message, another
    # session's traffic or a "ne" must never unlock a protected action.
    _APPROVAL_POSITIVE_WORDS = (
        "da", "moze", "potvrdujem", "potvrdi", "u redu", "ok", "okej",
        "tako je", "tocno", "svakako",
    )

    # A sentence that starts with "da" is not consent if it goes on to take it
    # back. "da, ali nemoj" and "da ne gasi bojler" both armed the old check:
    # first token "da", six words or fewer, no look at the rest.
    _APPROVAL_NEGATION_WORDS = frozenset({
        "ne", "nemoj", "nemojte", "necu", "nece", "ali", "ipak", "osim",
        "stani", "cekaj", "prekini", "odustani", "nikako", "nista",
    })

    @classmethod
    def _is_affirmative_reply(cls, normalized: str) -> bool:
        """Is this reply an unqualified yes?

        Anything else — a no, a hedge, an unrelated request — cancels, so the
        safe direction is the default: a reply we cannot read as plain consent
        does not authorise anything.
        """
        tokens = normalized.split()
        if any(token in cls._APPROVAL_NEGATION_WORDS for token in tokens):
            return False
        if normalized in cls._APPROVAL_POSITIVE_WORDS:
            return True
        return (
            bool(tokens)
            and tokens[0] in ("da", "moze", "potvrdujem")
            and len(tokens) <= 6
        )

    def _note_turn_start(self, session_id: str, message: str) -> None:
        """Record what this session began, so a later message can be told."""
        self._inflight_turns[session_id] = {
            "started_at": time.time(),
            "message": (message or "").strip(),
        }

    def _note_turn_end(self, session_id: str) -> None:
        self._inflight_turns.pop(session_id, None)

    def busy_notice(self, session_id: str) -> str:
        """What to say to someone who wrote while the previous turn runs.

        Says what is running, for how long, and -- the part that matters --
        that THIS message was not taken. Queueing it silently would answer a
        question minutes after it stopped being the one on the user's mind.
        """
        entry = self._inflight_turns.get(session_id)
        if not entry:
            return "Još radim na prethodnom zahtjevu. Javi se ponovno za koji trenutak."
        elapsed = int(time.time() - entry["started_at"])
        minutes, seconds = divmod(max(elapsed, 0), 60)
        how_long = f"{minutes} min {seconds} s" if minutes else f"{seconds} s"
        what = entry["message"]
        if len(what) > 60:
            what = what[:60].rsplit(" ", 1)[0] + "…"
        return (
            f"Još radim na prethodnom zahtjevu ({what}) — traje {how_long}. "
            "Ovu poruku nisam preuzeo; pošalji je ponovno kad javim da sam gotov."
        )

    async def acquire_or_busy(self, lock, session_id: str) -> bool:
        """Take the session lock, or give up and let the caller say so.

        The grace period exists so two quick messages still just queue --
        being told "I'm busy" after three seconds would be worse than waiting.
        Past it, the honest answer is that the session is occupied.
        """
        try:
            await asyncio.wait_for(
                lock.acquire(), timeout=turn_busy_notice_after_seconds()
            )
            return True
        except asyncio.TimeoutError:
            logger.info(
                "Session %s is busy; answering with a status instead of queueing",
                session_id,
            )
            return False

    def _process_turn_approvals(self, session_id: str, message: str) -> None:
        from services import approvals

        # Bind this turn's tool calls (register/redeem) to the session.
        approvals.set_session(session_id)

        # Asking once per turn is fine; it is the repeat inside one turn that
        # cannot succeed and must be interrupted. The ids come back rather
        # than being thrown away: a bare "da" may arm only what the gate
        # actually stopped to ask about.
        from services.approval_gate import take_holds
        held_actions = take_holds(session_id)

        # Same idea for a worker that keeps coming back empty: the count is
        # per turn, so a new message gives it a clean start.
        try:
            from agents.adk_agents.control_callbacks import reset_empty_results
            reset_empty_results(session_id)
        except Exception:
            logger.debug("Could not reset empty-result counters", exc_info=True)

        # Read before arming: on_user_turn consumes and cancels.
        had_pending = approvals.has_pending(session_id)
        lane = approvals.pending_lane(session_id)

        normalized = self._normalize_voice_text(message).strip(" ?!.,")
        affirmative = self._is_affirmative_reply(normalized)

        # One call covers both kinds of pending action: proposed meeting slots
        # arm on any turn (choosing IS the answer), yes/no approvals only on a
        # yes — and only the most recent one, so a single "da" cannot authorise
        # a queue of pending writes.
        armed = approvals.on_user_turn(
            session_id, affirmative=affirmative, held_actions=held_actions
        )

        if not had_pending:
            return

        # A pending approval means an agent just asked a question — route this
        # short reply back to the agent that asked (one-shot pin). Pinning every
        # smart-home turn was too broad: an unrelated short question minutes
        # after "upali svjetlo" landed in the wrong agent.
        if lane:
            self._voice_pinned_lane[session_id] = (
                lane, time.time() + VOICE_LANE_PIN_TTL_SECONDS
            )

        if armed:
            logger.info("Protected-action approval ARMED for session %s", session_id)
        elif affirmative:
            logger.info(
                "Affirmative reply in session %s, but nothing was waiting on a yes",
                session_id,
            )
        else:
            logger.info(
                "Pending protected-action approval CANCELLED for session %s "
                "(non-affirmative reply)", session_id,
            )


    def _apply_voice_lane_pin(
        self,
        session_id: str,
        message: str,
        route_type: str,
        route_target: Optional[str],
    ) -> tuple[str, Optional[str]]:
        """Sticky lane for write confirmations.

        Routing is re-classified every turn, so the "da" that answers
        "Predlažem utorak u 10. Potvrđuješ?" has no meeting keyword and would
        fall into voice_qa — losing the pending confirmation. While a pin is
        fresh, short follow-ups (confirmations, disambiguation answers like
        "onaj prvi") are redirected back to the pinned agent. Any turn that
        routes elsewhere voids the pin: the topic changed.
        """
        now = time.time()
        pinned = self._voice_pinned_lane.get(session_id)
        if pinned and now >= pinned[1]:
            self._voice_pinned_lane.pop(session_id, None)
            pinned = None

        if pinned and route_type == "agent" and route_target == "voice_qa":
            normalized = self._normalize_voice_text(message)
            tokens = [t.strip(".,!?") for t in normalized.split()]
            if tokens and (len(tokens) <= 6 or tokens[0] in VOICE_CONFIRMATION_WORDS):
                logger.info("Voice lane pin -> %s (short follow-up)", pinned[0])
                route_type, route_target = "agent", pinned[0]

        # Lanes with multi-turn confirmations: meeting scheduling ("da" /
        # "onaj prvi" must return to the pending flow).
        # smart_home is NOT pinned per-turn — its confirmations are routed
        # via the pending-approval one-shot pin in _process_turn_approvals.
        if route_type == "agent" and route_target == "secretary":
            self._voice_pinned_lane[session_id] = (
                route_target, now + VOICE_LANE_PIN_TTL_SECONDS
            )
        else:
            self._voice_pinned_lane.pop(session_id, None)
        return route_type, route_target

    async def _try_fast_smart_home_response(
        self,
        message: str,
        response_mode: Optional[str] = None,
    ) -> Optional[str]:
        # Single source of truth: services/voice_fast_path.py (this method
        # previously duplicated the matching logic, and the copies drifted).
        return await execute_fast_smart_home_command(
            message,
            response_mode=response_mode,
        )

    async def _run_direct_worker_agent(
        self,
        agent_name: str,
        user_id: str,
        session_id: str,
        message: str,
        ctx: "TurnContext",
        route_hint: Optional[str] = None,
        response_mode: Optional[str] = None,
    ) -> str:
        """Run a selected worker agent directly, bypassing orchestrator."""
        if agent_name == "smart_home":
            fast_response = await self._try_fast_smart_home_response(
                message,
                response_mode=response_mode,
            )
            if fast_response is not None:
                logger.info("Direct voice route -> smart_home_fast")
                return fast_response

        agent = self._get_worker_agent(agent_name)
        if agent is None:
            logger.warning(
                "Direct voice routing target '%s' not loaded; falling back to orchestrator",
                agent_name,
            )
            from config.voice_persona import wrap_agent_voice_message
            return await ctx.helper.run(wrap_agent_voice_message(message))

        from agents.adk_agents.runner_utils import run_agent_simple

        session_service = getattr(ctx.helper, "session_service", None)

        # Keep per-agent ADK sessions isolated. Voice follow-up continuity is
        # handled at the wakeword/router layer; sharing one ADK session across
        # different direct worker agents produced hung runs and unknown-agent
        # events when switching lanes.
        worker_session_id = f"{session_id}-{agent_name}"

        logger.info("Direct voice route -> %s", agent_name)
        # Persona is injected HERE, at the LLM boundary — routing and the
        # deterministic fast path above operate on the raw transcript only
        # (persona text once tripped the smart-home intent gate).
        from config.voice_persona import build_voice_persona_preamble

        persona = build_voice_persona_preamble()
        worker_message = f"{persona}\n\n{message}"
        if agent_name == "smart_home":
            worker_message = (
                f"{persona}\n\n"
                "Voice smart-home mode. Interpret the request, execute the home action if the "
                "tools allow it, and answer in one short Croatian sentence suitable for spoken output only when a spoken confirmation is necessary. "
                "If clarification is required, ask only one concise follow-up question.\n\n"
                f"User request: {message}"
            )
        elif agent_name == "secretary":
            worker_message = (
                f"{persona}\n\n"
                "Voice utility mode. Answer in Croatian with a short spoken-friendly response. "
                "Prefer one sentence unless the user explicitly asks for more detail.\n\n"
                f"User request: {message}"
            )
        elif agent_name == "voice_qa":
            worker_message = (
                f"{persona}\n\n"
                "Voice Q&A mode. Answer the user's question directly in Croatian. "
                "Keep it concise, useful, and suitable for spoken output. "
                "Prefer one short paragraph or at most two short sentences unless the user explicitly asks for depth. "
                "If the user is asking you to PERFORM an action (email, calendar, documents, "
                "smart home, scheduling), reply with exactly [[ESCALATE]] per your instructions.\n\n"
                f"User question: {message}"
            )
        # Same net as the orchestrator path. This lane is the FAST one, but
        # fast is not guaranteed: "pusti Arena Sport 1" spent 23.6s waiting
        # for the A1 Xplore app alone and answered after 40s, by which time
        # Assist on the phone had stopped listening -- and with no job
        # created, no notification came either. The TV had switched; the
        # user just never heard about it.
        response = await self._defer_to_phone_if_slow(
            lambda: run_agent_simple(
                agent,
                worker_message,
                session_id=worker_session_id,
                user_id=user_id,
                session_service=session_service,
                app_name="agents",
            ),
            user_id=user_id,
            question=message,
            ctx=ctx,
        )
        # NOTE: the LLM agent's answer is returned verbatim — response-mode
        # squashing ("U redu.") applies only to fast-path success texts.
        # Squashing here once hid confirmation questions and MQTT timeouts.

        # Phase 2: voice_qa has no tools; when it flags an action request with
        # the [[ESCALATE]] sentinel, re-run the original message through the
        # full orchestrator so the task actually gets executed.
        if agent_name == "voice_qa" and ESCALATE_SENTINEL in response[:200]:
            logger.info("voice_qa escalated to orchestrator: %r", message[:120])
            await self._speak_working_ack()
            from config.voice_persona import wrap_agent_voice_message
            return await self._run_orchestration_for_voice(
                wrap_agent_voice_message(message),
                user_id=user_id,
                question=message,
                # ctx is required, and omitting it made EVERY voice_qa
                # escalation raise TypeError before the orchestrator ran:
                # the user heard "tehnička greška" for any spoken request
                # that voice_qa handed on. Seen 2026-09-06 on "dodaj mi na
                # kupovnu listu kruh, ulje i mlijeko".
                ctx=ctx,
            )

        return response

    def _session_service_for_runner(self):
        """ADK session service a freshly built RunnerHelper should use.

        The base keeps whatever the system's seed helper has, so a channel
        that never configured one is unaffected. WebInterface overrides this
        with its shared service.
        """
        return getattr(
            getattr(self.system, "orchestrator_helper", None), "session_service", None
        )

    def _helper_for(self, session_id: str, user_id: str):
        """This session's runner, built once and kept.

        A cache rather than a single "current" attribute: two sessions have
        two runners at the same time, and neither can overwrite the other.
        Bounded, because sessions are cheap to make and a long-lived web
        process would otherwise keep every one it ever saw.
        """
        helper = self._runner_helpers.get(session_id)
        if helper is not None:
            return helper

        seed = getattr(self.system, "orchestrator_helper", None)
        if seed is not None and seed.session_id == session_id:
            self._runner_helpers[session_id] = seed
            return seed

        orchestrator = getattr(self.system, "orchestrator", None)
        if orchestrator is None:
            # Nothing to build a runner from, and the seed belongs to another
            # session. Handing it back would quietly run this request in that
            # session — exactly the confusion this cache exists to end. A
            # half-initialised system is a bug, so say so here rather than
            # letting it surface as a conversation in the wrong place.
            raise RuntimeError(
                f"No orchestrator to build a runner for session '{session_id}' "
                f"(system has no agents initialised)"
            )

        from agents.adk_agents.runner_utils import RunnerHelper
        logger.info("Creating RunnerHelper for session '%s'", session_id)
        helper = RunnerHelper(
            agent=orchestrator,
            session_id=session_id,
            user_id=user_id,
            app_name="agents",
            session_service=self._session_service_for_runner(),
        )

        while len(self._runner_helpers) >= self._MAX_CACHED_HELPERS:
            self._runner_helpers.pop(next(iter(self._runner_helpers)))
        self._runner_helpers[session_id] = helper
        return helper

    async def _prepare_turn(
        self,
        user_id: str,
        message: str,
        session_id: Optional[str] = None,
    ) -> "TurnContext":
        """Everything a turn needs before the agent runs — for every channel.

        Streaming used to do its own half of this and skip the approval half
        entirely: the session ContextVar was never set, so nothing ever armed
        and a "da" in the web UI could not authorise anything. One function,
        every door, so a protected action behaves the same whichever one the
        request came through.

        Returns the context to pass down. Callers must use what they are given
        rather than reading `self.system.*` — see TurnContext.
        """
        if self.system is None:
            self.initialize_system()

        if session_id is None:
            session_id = self.generate_session_id(user_id)

        # Protected-action approvals are session-bound and require an
        # EXPLICIT positive reply: "da"/"može" arms this session's pending
        # approvals; "ne"/anything else cancels them. The model can never
        # self-approve — only a real user message routes through here.
        # Meeting slot proposals are armed by any new user turn (choosing a
        # slot is free-text, so the create-gate itself checks the match).
        try:
            self._process_turn_approvals(session_id, message)
        except Exception:
            logger.debug("Turn approval processing failed", exc_info=True)

        try:
            from services import approvals
            approvals.set_user(user_id)
        except Exception:
            logger.debug("Could not bind turn user", exc_info=True)

        # Start the run ledger here, at the boundary every channel crosses.
        # It only ever started in the scheduler, so in ordinary chat nothing
        # recorded what the gate held or what a tool reported — and the step
        # contract that reads it was measuring an empty ledger.
        try:
            from services import run_effects
            run_effects.start_run()
        except Exception:
            logger.debug("Could not start the run ledger", exc_info=True)

        # Display only. Nothing dispatches on this.
        self.last_session_id = session_id
        self.last_user_id = user_id

        return TurnContext(
            session_id=session_id,
            user_id=user_id,
            helper=self._helper_for(session_id, user_id),
        )

    def _handle_classroom_command(self, message: str) -> Optional[str]:
        """Explicit entry/exit for the global Philosophy Classroom.

        Returns the reply when the message IS the command, otherwise None.
        """
        command = self._normalize_voice_text(message).strip(" ?!.,")
        if command in CLASSROOM_ENTER_COMMANDS:
            self.system.active_mode = "CLASSROOM"
            return (
                "Ulazim u filozofsku učionicu. Sokrat vodi razgovor — "
                "za izlaz reci „izađi iz učionice”."
            )
        if command in CLASSROOM_LEAVE_COMMANDS:
            self.system.active_mode = "LEGACY"
            return "Izlazim iz filozofske učionice. Natrag na normalan rad."
        return None

    async def process_message(
        self,
        user_id: str,
        message: str,
        session_id: Optional[str] = None,
        route_hint: Optional[str] = None,
        response_mode: Optional[str] = None,
    ) -> str:
        """
        Process a user message through the agent system.

        Args:
            user_id: Unique identifier for the user
            message: The user's message text
            session_id: Optional existing session ID

        Returns:
            Agent response string
        """
        ctx = await self._prepare_turn(user_id, message, session_id)
        session_id = ctx.session_id

        _token_marker = None
        try:
            from tools.observability.token_stats import get_token_stats, token_stats_enabled
            if token_stats_enabled():
                _token_marker = get_token_stats().mark()
        except Exception:
            _token_marker = None

        try:
            explicit_mode_reply = self._handle_classroom_command(message)
            if explicit_mode_reply is not None:
                return explicit_mode_reply

            # Check for mode routing (CLASSROOM vs LEGACY)
            if self.system.active_mode == "CLASSROOM" and not self._should_use_voice_direct_routing(user_id):
                result = await self._process_classroom_mode(message)
            else:
                direct_agent = "socrates" if looks_philosophical(message) else None

                if self._should_use_voice_direct_routing(user_id):
                    # Asking after a long task must never start another one.
                    if self._is_job_status_question(message):
                        status = self._answer_job_status(session_id)
                        if status is not None:
                            return status

                    if route_hint in {"smart_home", "voice_qa", "christian_guide", "socrates", "secretary"}:
                        route_type, route_target = "agent", route_hint
                        logger.info("Pinned voice route -> %s", route_target)
                    else:
                        route_type, route_target = self._classify_voice_route(message)
                    route_type, route_target = self._apply_voice_lane_pin(
                        session_id, message, route_type, route_target
                    )

                    if route_type == LOCAL_VOICE_ROUTE:
                        result = self._build_time_or_date_response(message)
                    elif route_type == WEATHER_VOICE_ROUTE:
                        result = await self._build_weather_response(message)
                    elif route_type == BRIEFING_VOICE_ROUTE:
                        result = await self._build_briefing_response()
                    elif route_type == "agent" and route_target:
                        result = await self._run_direct_worker_agent(
                            route_target,
                            user_id=user_id,
                            session_id=session_id,
                            message=message,
                            ctx=ctx,
                            route_hint=route_hint,
                            response_mode=response_mode,
                        )
                    else:
                        # Multi-step orchestrator run — tell the user we're on
                        # it before minutes of silent work. Persona goes in at
                        # this LLM boundary (routing above saw the raw text).
                        await self._speak_working_ack()
                        from config.voice_persona import wrap_agent_voice_message
                        result = await self._run_orchestration_for_voice(
                            wrap_agent_voice_message(message),
                            user_id=user_id,
                            question=message,
                            ctx=ctx,
                        )
                elif direct_agent == "socrates":
                    # One turn in the philosophy lane, not a mode switch.
                    # active_mode is global, so a keyword flipping it from one
                    # channel left every other channel answering as Socrates —
                    # and the web stream deadlocked on the way in.
                    result = await self._process_classroom_mode(message)
                else:
                    result = await self._orchestrate(ctx)(message)

            return result

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            # Voice users get a short spoken-Croatian failure with the rough
            # cause; other channels keep the raw error for debugging.
            if user_id.startswith(SPOKEN_CHANNEL_USER_PREFIXES):
                return self._voice_friendly_error(e)
            return f"Error processing request: {str(e)}"
        finally:
            if _token_marker is not None:
                try:
                    from tools.observability.token_stats import get_token_stats
                    report = get_token_stats().report(
                        start=_token_marker,
                        title=f"TOKEN USAGE (turn) user={user_id}",
                    )
                    logger.info("[TOKENS]\n%s", report)
                except Exception:
                    pass

    async def _process_classroom_mode(self, message: str, first_entry: bool = False) -> str:
        """
        Process message in Philosophy Classroom mode.

        Args:
            message: User message
            first_entry: Whether this is first entry to classroom

        Returns:
            Socrates response
        """
        prefix = "Entering Philosophy Classroom...\n\n" if first_entry else ""

        # Run Socrates
        socrates_response = await self.system.socrates.run_with_fallback(message)

        # Check for termination
        check_input = f"User input: {message}\nSocrates response: {socrates_response}"
        check_response = await self.system.termination_checker.run_with_fallback(check_input)

        if "TERMINATE" in check_response:
            self.system.active_mode = "LEGACY"
            return f"{prefix}{socrates_response}\n\n[Class dismissed. Returning to normal mode.]"

        return f"{prefix}{socrates_response}"

    def get_status(self) -> Dict[str, Any]:
        """
        Get system status information.

        Returns:
            Dictionary with status information
        """
        if self.system is None:
            return {"status": "not_initialized"}

        from config.agent_registry import get_registry_stats
        stats = get_registry_stats()

        return {
            "status": "running",
            "interface": self.session_prefix,
            "active_mode": self.system.active_mode,
            # The interface's own last session. system.session_id is the
            # CLI's, and no longer follows web or voice traffic.
            "session_id": self.last_session_id or self.system.session_id,
            "total_agents": stats['total_agents'],
            "worker_agents": stats['worker_agents'],
            "adk_migration": stats['adk_migration_progress']
        }

    def get_agents_info(self) -> str:
        """
        Get formatted information about available agents.

        Returns:
            Formatted string with agent information
        """
        if self.system is None:
            return "System not initialized"

        lines = ["=== Available Agents ===\n"]

        # Orchestrator
        lines.append(f"Smart Orchestrator: {self.system.orchestrator.name}")
        lines.append(f"  Model: {self.system.orchestrator.model}")
        lines.append(f"  Features: Multi-agent coordination, conditional logic\n")

        # Enterprise components
        lines.append("Enterprise Components:")
        validator = getattr(self.system, "decision_validator", None)
        if validator:
            lines.append(f"  - Decision Validator ({validator.model})")
        else:
            lines.append("  - Decision Validator: disabled (no specialist sub-agents)")
        lines.append(f"  - Ask User Agent ({self.system.ask_user.model})\n")

        # Philosophy
        lines.append("Philosophy Classroom:")
        lines.append(f"  - Socrates ({self.system.socrates.model})")
        lines.append(f"  - TerminationChecker ({self.system.termination_checker.model})\n")

        # Workers
        lines.append(f"Worker Agents ({len(self.system.worker_agents)}):")
        for agent in self.system.worker_agents:
            lines.append(f"  - {agent.name} ({agent.model})")

        return "\n".join(lines)

    @abstractmethod
    async def start(self) -> None:
        """Start the interface. Must be implemented by subclasses."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the interface. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def format_response(self, response: str) -> str:
        """
        Format response for the specific interface.

        Args:
            response: Raw response from agent

        Returns:
            Formatted response for the interface
        """
        pass
