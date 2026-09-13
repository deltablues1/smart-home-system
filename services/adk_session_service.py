"""
Firestore-backed ADK Session Service
=====================================
Persists ADK session state (conversation history + tool context) to Firestore
so that agent context survives server restarts and session switches.

Feature flag: USE_PERSISTENT_ADK_SESSIONS=true  (default: false, but the Pi's
.env and both deploy profiles turn it on — this path is live)

Layout:
    adk_sessions/{session_id}                  state, event_count, revision
    adk_sessions/{session_id}/events/{0000-00000042}  one event per document

Three things the previous version got wrong, all of them silent:

* **The whole session went into one document on every event.** A conversation
  rewrote its entire history each time it grew, so N events cost O(N²) bytes
  written, and a long enough conversation would eventually hit Firestore's
  1 MiB document limit and simply stop persisting. Events are their own
  documents now, written once.
* **Writes raced.** Each append fired an unordered background task carrying a
  full snapshot, so a write holding 5 events could land after one holding 7
  and truncate the stored history. Writes are serialized per session and only
  ever append.
* **Nothing waited for them.** The task handle was discarded, so a shutdown
  dropped whatever was in flight and `close()` had no callers at all. Tasks
  are held and `flush()` awaits them.

Old single-document sessions are still readable and are migrated to the new
layout the first time they are touched.

Two rules make "reads back whole" mean something:

* **The state document is the commit point.** Events are written first, then
  the state document records how many of them count. A reader never looks past
  that number, so events from a write that died half way are invisible rather
  than half-joined to the previous ones.
* **A damaged history is never continued.** If an event is missing, appending
  to the same sequence would fill the hole with a NEW event and let the stale
  tail behind it come back as if it belonged — old0, NEW, old2. The coherent
  prefix is moved to a fresh generation instead, and everything after the gap
  stays where it is, out of the way but available for a look.

Ordinary process death still loses whatever had not been written yet. That is
not fixable here — the guarantee is that what *was* written reads back whole,
not that everything reaches disk.
"""

import os
import asyncio
import copy
import json
import logging
import uuid
from typing import Optional, Any
from datetime import datetime, timezone

from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.base_session_service import ListSessionsResponse, GetSessionConfig
from google.adk.events import Event
from google.cloud.firestore_v1.async_client import AsyncClient

logger = logging.getLogger(__name__)

_COLLECTION = "adk_sessions"
_EVENTS = "events"

# Bumped when the stored shape changes. Absent means the original layout,
# where the whole session lived in one "data" field.
_SCHEMA = 2


class FirestoreADKSessionService(BaseSessionService):
    """
    ADK SessionService backed by Firestore.

    Usage:
        service = FirestoreADKSessionService()
        runner = Runner(agent=agent, app_name="agents", session_service=service)
    """

    def __init__(self, project_id: Optional[str] = None):
        self._project_id = project_id or os.environ.get('GOOGLE_CLOUD_PROJECT')
        self._db: Optional[AsyncClient] = None
        # In-memory cache: session_id -> Session
        self._cache: dict[str, Session] = {}
        # How many of each session's events are already in Firestore. The
        # write path appends from here, so nothing is ever rewritten.
        self._persisted: dict[str, int] = {}
        self._revision: dict[str, int] = {}
        # Bumped when a damaged history has to be rewritten, so the repaired
        # events cannot collide with the ones they replace.
        self._generation: dict[str, int] = {}
        # One writer per session at a time. Without this, two appends raced
        # and the older snapshot could land last.
        self._locks: dict[str, asyncio.Lock] = {}
        # Strong references: a task nobody holds can be garbage collected
        # mid-write, and there would be nothing to await at shutdown.
        self._pending: set[asyncio.Task] = set()

    def _get_db(self) -> AsyncClient:
        if self._db is None:
            self._db = AsyncClient(project=self._project_id)
            logger.info(f"[ADKSessionService] Firestore client initialized (project={self._project_id})")
        return self._db

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # ------------------------------------------------------------------
    # BaseSessionService interface
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """
        Get existing session from cache/Firestore, or create a new one.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())

        # 1. Check in-memory cache first (cheapest)
        if session_id in self._cache:
            logger.debug(f"[ADKSessionService] Cache hit for '{session_id}'")
            return self._cache[session_id]

        # 2. Try to restore from Firestore
        restored = await self._load_from_firestore(session_id)
        if restored:
            self._cache[session_id] = restored
            logger.info(
                f"[ADKSessionService] Restored session '{session_id}' from Firestore "
                f"({len(restored.events)} events)"
            )
            return restored

        # 3. Create brand new session
        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=state or {},
            events=[],
            last_update_time=0.0,
        )
        self._cache[session_id] = session
        self._persisted[session_id] = 0
        self._revision[session_id] = 0
        self._generation[session_id] = 0
        # Persist immediately so session_id is "known" in Firestore
        await self._flush_session(session)
        logger.info(f"[ADKSessionService] Created new session '{session_id}'")
        return session

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        """Return session from cache or Firestore."""
        if session_id in self._cache:
            return self._cache[session_id]

        session = await self._load_from_firestore(session_id)
        if session:
            self._cache[session_id] = session
        return session

    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: Optional[str] = None,
    ) -> ListSessionsResponse:
        """Return cached sessions matching app_name / user_id."""
        sessions = [
            s for s in self._cache.values()
            if s.app_name == app_name and (user_id is None or s.user_id == user_id)
        ]
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """Remove session from cache and Firestore, events included."""
        known_events = self._persisted.pop(session_id, None)
        generation = self._generation.pop(session_id, 0)
        self._cache.pop(session_id, None)
        self._revision.pop(session_id, None)
        self._locks.pop(session_id, None)
        try:
            db = self._get_db()
            doc = db.collection(_COLLECTION).document(session_id)
            # Firestore does not cascade, so the event documents would
            # otherwise outlive the session that owned them.
            if known_events is None:
                snapshot = await doc.get()
                data = snapshot.to_dict() if snapshot.exists else None
                known_events = (data or {}).get("event_count", 0)
            for index in range(known_events):
                await doc.collection(_EVENTS).document(
                    _event_id(index, generation)
                ).delete()
            await doc.delete()
            logger.debug(f"[ADKSessionService] Deleted session '{session_id}'")
        except Exception as e:
            logger.error(f"[ADKSessionService] Failed to delete '{session_id}': {e}")

    async def append_event(self, session: Session, event: Event) -> Event:
        """
        Append event to session (in-memory) then persist in the background.
        Never blocks the caller — but the write is tracked, not abandoned.
        """
        event = await super().append_event(session, event)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g., sync tests) — skip persistence
            return event

        task = loop.create_task(self._flush_session(session))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _flush_session(self, session: Session) -> None:
        """Append this session's unwritten events, then update its state doc.

        Serialized per session and append-only, so two flushes racing cannot
        reorder anything: the second simply finds less to do.
        """
        async with self._lock_for(session.id):
            # One snapshot, taken before the first await, and independent
            # all the way down. The lock serializes writers; it does not stop
            # the agent mutating the session object while a write is in
            # flight. A shallow dict() left nested values shared, so a
            # snapshot could still change under the write that was storing it.
            #
            # Events are serialized here for the same reason: copying the
            # list only fixes the list.
            generation = self._generation.get(session.id, 0)
            start = self._persisted.get(session.id, 0)
            event_count = len(session.events)
            pending_events = [
                (index, session.events[index].model_dump_json())
                for index in range(start, event_count)
            ]
            state = _dump_state(session.state)
            last_update_time = session.last_update_time
            app_name = session.app_name
            user_id = session.user_id

            try:
                db = self._get_db()
                doc = db.collection(_COLLECTION).document(session.id)

                for index, payload in pending_events:
                    await doc.collection(_EVENTS).document(
                        _event_id(index, generation)
                    ).set({
                        "index": index,
                        "generation": generation,
                        "data": payload,
                    })

                # Written last on purpose: until this lands, the events above
                # are not part of the history a reader will see.
                revision = self._revision.get(session.id, 0) + 1
                await doc.set({
                    "session_id": session.id,
                    "app_name": app_name,
                    "user_id": user_id,
                    "state": state,
                    "event_count": event_count,
                    "generation": generation,
                    "last_update_time": last_update_time,
                    "revision": revision,
                    "schema": _SCHEMA,
                    "updated_at": datetime.now(timezone.utc),
                })

                self._persisted[session.id] = event_count
                self._revision[session.id] = revision
                logger.debug(
                    f"[ADKSessionService] Persisted '{session.id}' "
                    f"(rev {revision}, {event_count} events)"
                )
            except Exception as e:
                logger.error(f"[ADKSessionService] Failed to persist '{session.id}': {e}")

    async def _load_from_firestore(self, session_id: str) -> Optional[Session]:
        """Load a session. Returns None on miss or on an unreadable record."""
        try:
            db = self._get_db()
            doc = db.collection(_COLLECTION).document(session_id)
            snapshot = await doc.get()
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}

            if "data" in data:
                return await self._migrate_legacy(session_id, data)

            committed = data.get("event_count", 0)
            generation = data.get("generation", 0)
            events = await self._load_events(doc, committed, generation)

            self._generation[session_id] = generation
            self._persisted[session_id] = len(events)
            self._revision[session_id] = data.get("revision", 0)

            damaged = len(events) < committed
            session = Session(
                id=session_id,
                app_name=data.get("app_name", "agents"),
                user_id=data.get("user_id", ""),
                # A truncated history cannot keep the state that belonged to
                # the full one: it names things the conversation no longer
                # contains. An empty scratchpad next to a short but coherent
                # transcript is the only honest pairing available here.
                state={} if damaged else _load_state(data.get("state")),
                events=events,
                last_update_time=0.0 if damaged else data.get("last_update_time", 0.0),
            )

            if damaged:
                await self._start_new_generation(session, generation, committed)
            return session
        except Exception as e:
            logger.error(f"[ADKSessionService] Failed to load '{session_id}': {e}")
        return None

    async def _load_events(self, doc, committed: int, generation: int) -> list:
        """Read this generation's committed events, in index order.

        `committed` is the count the state document vouches for, and nothing
        past it is read: an event written by a flush that died before it
        updated the state document is not yet part of the history.

        Sorted here rather than by a Firestore query, so the order does not
        depend on document-id collation and a gap is visible.
        """
        rows = []
        async for snapshot in doc.collection(_EVENTS).stream():
            row = snapshot.to_dict() or {}
            if "data" not in row:
                continue
            if row.get("generation", 0) != generation:
                continue
            index = row.get("index", 0)
            if index >= committed:
                continue  # written, but not committed by the state document
            rows.append((index, row["data"]))
        rows.sort(key=lambda pair: pair[0])

        events = []
        for index, (stored_index, payload) in enumerate(rows):
            if stored_index != index:
                # A missing event means everything after it is out of context.
                # Stopping here returns a shorter but coherent conversation.
                logger.warning(
                    "[ADKSessionService] Event gap in '%s' at %d (found %d) — "
                    "history truncated there", doc.id, index, stored_index,
                )
                break
            events.append(Event.model_validate_json(payload))

        if committed and len(events) != committed:
            logger.warning(
                "[ADKSessionService] '%s' committed %d events, read %d",
                doc.id, committed, len(events),
            )
        return events

    async def _start_new_generation(
        self, session: Session, generation: int, committed: int
    ) -> None:
        """Move a repaired history out of the damaged sequence.

        Appending to the old sequence would write the new event into the gap
        and bring the stale tail back with it: old0, NEW, old2, all looking
        equally genuine. The coherent prefix is rewritten under a fresh
        generation and the damaged documents are left alone — they are still
        there to look at, they are simply no longer this conversation.
        """
        logger.error(
            "[ADKSessionService] '%s' is damaged (%d of %d events readable) — "
            "continuing in generation %d; the old documents are kept for "
            "inspection", session.id, len(session.events), committed, generation + 1,
        )
        self._generation[session.id] = generation + 1
        self._persisted[session.id] = 0
        await self._flush_session(session)

    async def _migrate_legacy(self, session_id: str, data: dict) -> Optional[Session]:
        """Read an old single-document session and rewrite it in the new layout.

        Kept rather than dropped: these are real conversations, and the flag
        has been on long enough on the Pi that discarding them would lose
        context people still refer back to.
        """
        try:
            session = Session.model_validate_json(data["data"])
        except Exception as e:
            logger.error(
                "[ADKSessionService] Could not read legacy session '%s': %s",
                session_id, e,
            )
            return None

        # The blob carries the id it was written with, and the write path
        # addresses documents by session.id — so migrating a document whose
        # stored id differs from the one being read wrote the result under the
        # OTHER document. Usually those agree and nothing shows; the case
        # where they do not is a copy, and a copy is exactly what you make
        # when you want to try a migration without touching the original.
        if session.id != session_id:
            logger.warning(
                "[ADKSessionService] Legacy session '%s' carries id '%s' — "
                "migrating under the id it was read as",
                session_id, session.id,
            )
            session = session.model_copy(update={"id": session_id})

        logger.info(
            "[ADKSessionService] Migrating '%s' from the single-document layout "
            "(%d events)", session_id, len(session.events),
        )
        self._persisted[session_id] = 0
        self._revision[session_id] = data.get("revision", 0)
        self._generation[session_id] = 0
        await self._flush_session(session)
        return session

    async def flush(self) -> None:
        """Wait for every in-flight write. Call before the process exits."""
        pending = [task for task in self._pending if not task.done()]
        if not pending:
            return
        logger.info("[ADKSessionService] Waiting for %d session write(s)", len(pending))
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Finish outstanding writes, then close the Firestore client."""
        await self.flush()
        if self._db is not None:
            self._db.close()
            self._db = None
            logger.info("[ADKSessionService] Firestore client closed")


def _event_id(index: int, generation: int = 0) -> str:
    """Zero-padded so document ids sort the way the events happened.

    The generation prefix is what keeps a repaired history from colliding
    with the damaged one it replaced.
    """
    return f"{generation:04d}-{index:08d}"


def _dump_state(state: Any) -> dict:
    """An independent copy of the session state.

    A JSON round trip rather than dict(): dict() detaches only the top level,
    so a nested dict or list stayed shared with the live session and could
    change under the write that was storing it.

    `default=str` means a value JSON cannot represent is stored as its string
    form rather than refused — lossy, and deliberately so: dropping a session
    write because one scratch value is an odd type would lose the whole
    conversation to save one field. deepcopy is the fallback when the round
    trip fails outright.

    The last fallback, a shallow copy, is NOT an independent snapshot. It is
    there so an uncopyable state still gets stored rather than nothing, and it
    logs an error, because at that point the guarantee is gone.
    """
    if not state:
        return {}
    try:
        return json.loads(json.dumps(dict(state), default=str))
    except Exception:
        logger.warning(
            "[ADKSessionService] State is not JSON-clean; deep-copying instead",
            exc_info=True,
        )
        try:
            return copy.deepcopy(dict(state))
        except Exception:
            logger.error(
                "[ADKSessionService] State could not be copied; storing a "
                "shallow copy, which may change under the write",
                exc_info=True,
            )
            return dict(state)


def _load_state(stored: Any) -> dict:
    return dict(stored or {})
