"""
Conversation history must survive a restart without rewriting itself to death.

The old layout put the whole session in one Firestore document and wrote it
again on every event. Three consequences, all silent:

* N events cost O(N²) bytes, and a long enough conversation would hit the
  1 MiB document limit and simply stop persisting;
* the background writes were unordered, so a snapshot holding 5 events could
  land after one holding 7 and truncate what was stored;
* nothing held or awaited those tasks, so a shutdown dropped whatever was in
  flight — and `close()` had no callers at all.

Events are their own documents now, appended once, serialized per session, and
awaited at shutdown. Sessions written in the old layout are migrated when they
are first touched, because they are real conversations people refer back to.

No Firestore — an in-memory double stands in for it.

Run with:
    pytest tests/unit/test_adk_session_service.py -v
"""

import asyncio

import pytest

from google.adk.events import Event

import services.adk_session_service as adk_session_service
from services.adk_session_service import FirestoreADKSessionService


# --- an in-memory stand-in for Firestore ------------------------------------

class _Snapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path, stats):
        self._store = store
        self._path = path
        self._stats = stats
        self.id = path[-1]

    async def set(self, data):
        self._stats["writes"] += 1
        self._stats["written_paths"].append("/".join(self._path))
        if self._stats["write_delay"]:
            await asyncio.sleep(self._stats["write_delay"])
        self._store[tuple(self._path)] = dict(data)

    async def get(self):
        self._stats["reads"] += 1
        return _Snapshot(self._store.get(tuple(self._path)))

    async def delete(self):
        self._store.pop(tuple(self._path), None)

    def collection(self, name):
        return _Collection(self._store, self._path + [name], self._stats)


class _Collection:
    def __init__(self, store, path, stats):
        self._store = store
        self._path = path
        self._stats = stats

    def document(self, doc_id):
        return _Doc(self._store, self._path + [doc_id], self._stats)

    async def stream(self):
        prefix = tuple(self._path)
        for key in sorted(self._store):
            if len(key) == len(prefix) + 1 and key[:len(prefix)] == prefix:
                yield _Snapshot(self._store[key])


class _FakeFirestore:
    def __init__(self, project=None):
        self.store = {}
        self.stats = {
            "writes": 0, "reads": 0, "written_paths": [], "write_delay": 0.0,
        }
        self.closed = False

    def collection(self, name):
        return _Collection(self.store, [name], self.stats)

    def close(self):
        self.closed = True


@pytest.fixture
def db(monkeypatch):
    fake = _FakeFirestore()
    monkeypatch.setattr(
        adk_session_service, "AsyncClient", lambda project=None: fake
    )
    return fake


@pytest.fixture
def service(db):
    return FirestoreADKSessionService(project_id="test")


def _event(text):
    return Event(author="user", invocation_id=text)


async def _new_session(service, session_id="s1"):
    return await service.create_session(
        app_name="agents", user_id="ana", session_id=session_id
    )


def _state_doc(db, session_id="s1"):
    return db.store.get(("adk_sessions", session_id))


def _event_docs(db, session_id="s1"):
    prefix = ("adk_sessions", session_id, "events")
    return [
        db.store[k] for k in sorted(db.store)
        if len(k) == 4 and k[:3] == prefix
    ]


# --- the shape on disk ------------------------------------------------------

class TestEventsAreTheirOwnDocuments:

    @pytest.mark.asyncio
    async def test_the_state_document_does_not_hold_the_history(self, service, db):
        session = await _new_session(service)
        for n in range(3):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        state = _state_doc(db)
        assert state["event_count"] == 3
        assert "data" not in state, "the whole session is back in one document"
        assert len(_event_docs(db)) == 3

    @pytest.mark.asyncio
    async def test_an_event_is_written_once_not_rewritten(self, service, db):
        session = await _new_session(service)
        await service.flush()

        for n in range(5):
            await service.append_event(session, _event(f"e{n}"))
            await service.flush()

        event_writes = [
            p for p in db.stats["written_paths"] if "/events/" in p
        ]
        # Five events, five event writes. The old layout wrote the growing
        # history again every time: 1+2+3+4+5.
        assert len(event_writes) == 5
        assert len(set(event_writes)) == 5

    @pytest.mark.asyncio
    async def test_the_revision_advances(self, service, db):
        session = await _new_session(service)
        first = _state_doc(db)["revision"]

        await service.append_event(session, _event("e"))
        await service.flush()

        assert _state_doc(db)["revision"] > first


# --- ordering -----------------------------------------------------------------

class TestWritesCannotReorder:

    @pytest.mark.asyncio
    async def test_a_slow_write_cannot_truncate_a_later_one(self, service, db):
        session = await _new_session(service)
        await service.flush()

        # Every write is slow enough that the flushes genuinely overlap.
        db.stats["write_delay"] = 0.01
        for n in range(6):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()
        db.stats["write_delay"] = 0.0

        # Before the per-session lock, an earlier snapshot could land last and
        # leave event_count below what had already been stored.
        assert _state_doc(db)["event_count"] == 6
        assert len(_event_docs(db)) == 6


# --- round trip ---------------------------------------------------------------

class TestRestore:

    @pytest.mark.asyncio
    async def test_a_flushed_session_reads_back_whole(self, service, db):
        session = await _new_session(service)
        for n in range(4):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        assert restored is not None
        assert [e.invocation_id for e in restored.events] == ["e0", "e1", "e2", "e3"]
        assert restored.user_id == "ana"

    @pytest.mark.asyncio
    async def test_restoring_does_not_rewrite_what_is_already_there(
        self, service, db
    ):
        session = await _new_session(service)
        for n in range(3):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )
        db.stats["written_paths"].clear()

        await fresh.append_event(restored, _event("e3"))
        await fresh.flush()

        event_writes = [p for p in db.stats["written_paths"] if "/events/" in p]
        assert event_writes == ["adk_sessions/s1/events/0000-00000003"]

    @pytest.mark.asyncio
    async def test_a_gap_truncates_rather_than_reordering(self, service, db):
        session = await _new_session(service)
        for n in range(4):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        # Lose event 2. Everything after it is out of context, so a coherent
        # short history beats a complete-looking wrong one.
        del db.store[("adk_sessions", "s1", "events", "0000-00000002")]

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        assert [e.invocation_id for e in restored.events] == ["e0", "e1"]


# --- shutdown -----------------------------------------------------------------

class TestShutdown:

    @pytest.mark.asyncio
    async def test_close_waits_for_writes_in_flight(self, service, db):
        session = await _new_session(service)
        db.stats["write_delay"] = 0.02
        for n in range(3):
            await service.append_event(session, _event(f"e{n}"))

        # No flush() first: close() must do the waiting itself. The previous
        # version discarded the task handles, so there was nothing to wait on.
        await service.close()

        assert _state_doc(db)["event_count"] == 3
        assert db.closed is True

    @pytest.mark.asyncio
    async def test_an_abrupt_stop_leaves_readable_history(self, service, db):
        session = await _new_session(service)
        db.stats["write_delay"] = 0.05
        for n in range(3):
            await service.append_event(session, _event(f"e{n}"))

        # Power cut: nothing is awaited, the process is gone.
        await asyncio.sleep(0.06)
        for task in list(service._pending):
            task.cancel()

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        # The promise is not "complete" — it is "whole as far as it goes".
        assert restored is not None
        ids = [e.invocation_id for e in restored.events]
        assert ids == [f"e{n}" for n in range(len(ids))]


# --- the old layout -----------------------------------------------------------

class TestLegacyMigration:

    @pytest.mark.asyncio
    async def test_an_old_single_document_session_is_read_and_migrated(
        self, service, db
    ):
        # Seed a session exactly as the previous version wrote it.
        seed = FirestoreADKSessionService(project_id="test")
        old = await seed.create_session(
            app_name="agents", user_id="ana", session_id="old-1"
        )
        for n in range(3):
            await seed.append_event(old, _event(f"e{n}"))
        await seed.flush()

        legacy_json = old.model_dump_json()
        db.store.clear()
        db.store[("adk_sessions", "old-1")] = {
            "session_id": "old-1",
            "app_name": "agents",
            "user_id": "ana",
            "data": legacy_json,
            "event_count": 3,
        }

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="old-1"
        )

        assert restored is not None
        assert [e.invocation_id for e in restored.events] == ["e0", "e1", "e2"]

        # And it is now stored the new way, so it never pays the O(n²) cost
        # or the 1 MiB ceiling again.
        state = db.store[("adk_sessions", "old-1")]
        assert "data" not in state
        assert state["schema"] == 2
        assert len(_event_docs(db, "old-1")) == 3

    @pytest.mark.asyncio
    async def test_an_unreadable_legacy_record_is_a_miss_not_a_crash(
        self, service, db
    ):
        db.store[("adk_sessions", "broken")] = {"data": "{not json"}

        restored = await service.get_session(
            app_name="agents", user_id="ana", session_id="broken"
        )

        assert restored is None


class TestDelete:

    @pytest.mark.asyncio
    async def test_deleting_takes_the_events_with_it(self, service, db):
        session = await _new_session(service)
        for n in range(3):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        await service.delete_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        # Firestore does not cascade; orphaned event documents would outlive
        # the session that owned them.
        assert _state_doc(db) is None
        assert _event_docs(db) == []


class TestADamagedHistoryIsNeverContinued:
    """Truncating at the gap is not enough on its own.

    The documents after the gap are still there. Appending to the same
    sequence writes the new event into the hole, and the next read walks
    straight through it into the stale tail:

        stored:  old0, (missing), old2
        read:    old0
        append:  old0, NEW
        reread:  old0, NEW, old2      <- old2 is back, looking genuine
    """

    async def _damaged(self, service, db):
        session = await _new_session(service)
        for n in range(3):
            await service.append_event(session, _event(f"old{n}"))
        session.state["last"] = "old2"
        await service.flush()
        del db.store[("adk_sessions", "s1", "events", "0000-00000001")]

    @pytest.mark.asyncio
    async def test_the_stale_tail_does_not_come_back(self, service, db):
        await self._damaged(service, db)

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )
        assert [e.invocation_id for e in restored.events] == ["old0"]

        await fresh.append_event(restored, _event("NEW"))
        await fresh.flush()

        after = FirestoreADKSessionService(project_id="test")
        reread = await after.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )
        assert [e.invocation_id for e in reread.events] == ["old0", "NEW"]

    @pytest.mark.asyncio
    async def test_the_repair_moves_to_a_new_generation(self, service, db):
        await self._damaged(service, db)

        fresh = FirestoreADKSessionService(project_id="test")
        await fresh.get_session(app_name="agents", user_id="ana", session_id="s1")

        assert _state_doc(db)["generation"] == 1
        # The damaged documents are left alone — still there to look at, no
        # longer part of this conversation.
        assert ("adk_sessions", "s1", "events", "0000-00000002") in db.store
        assert ("adk_sessions", "s1", "events", "0001-00000000") in db.store

    @pytest.mark.asyncio
    async def test_the_state_does_not_outlive_the_history_it_described(
        self, service, db
    ):
        await self._damaged(service, db)

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        # A state naming "old2" next to a history that stops at "old0" is the
        # inconsistency, not a convenience worth keeping.
        assert restored.state == {}
        assert _state_doc(db)["state"] == {}


class TestTheStateDocumentIsTheCommitPoint:

    @pytest.mark.asyncio
    async def test_events_past_the_committed_count_are_not_read(self, service, db):
        session = await _new_session(service)
        for n in range(2):
            await service.append_event(session, _event(f"e{n}"))
        await service.flush()

        # A flush that wrote its events and died before updating the state
        # document. Those events are not history yet.
        db.store[("adk_sessions", "s1", "events", "0000-00000002")] = {
            "index": 2, "generation": 0, "data": _event("half-written").model_dump_json(),
        }

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="s1"
        )

        assert [e.invocation_id for e in restored.events] == ["e0", "e1"]

    @pytest.mark.asyncio
    async def test_state_and_events_come_from_one_moment(self, service, db):
        session = await _new_session(service)
        await service.flush()

        db.stats["write_delay"] = 0.05
        session.state["last"] = "one"
        first = asyncio.ensure_future(
            service.append_event(session, _event("e0"))
        )
        await asyncio.sleep(0.01)

        # The agent carries on while the write is in flight. The lock stops
        # two writers overlapping; it does not stop the session object
        # changing underneath one of them.
        session.state["last"] = "two"
        session.events.append(_event("e1"))

        await first
        await service.flush()
        db.stats["write_delay"] = 0.0

        stored = _state_doc(db)
        # Whatever pair is stored, it must be a pair that actually existed:
        # never event_count=1 beside a state from two turns later.
        if stored["event_count"] == 1:
            assert stored["state"]["last"] == "one"
        else:
            assert stored["state"]["last"] == "two"


class TestTheSnapshotGoesAllTheWayDown:
    """`dict(state)` detaches the top level and nothing else.

    A nested dict or list stayed shared with the live session, so the agent
    changing it mid-write changed the snapshot that was being stored — the
    same inconsistency the snapshot exists to prevent, one level down.
    """

    @pytest.mark.asyncio
    async def test_a_nested_change_during_a_write_does_not_reach_the_store(
        self, service, db
    ):
        session = await _new_session(service)
        session.state["nested"] = {"step": 1}
        await service.flush()

        db.stats["write_delay"] = 0.05
        flush = asyncio.ensure_future(
            service.append_event(session, _event("e0"))
        )
        await asyncio.sleep(0.01)

        # The agent works on while the write is in flight.
        session.state["nested"]["step"] = 2

        await flush
        await service.flush()
        db.stats["write_delay"] = 0.0

        stored = _state_doc(db)
        if stored["event_count"] == 1:
            assert stored["state"]["nested"]["step"] == 1
        else:
            assert stored["state"]["nested"]["step"] == 2

    @pytest.mark.asyncio
    async def test_a_list_in_the_state_is_detached_too(self, service, db):
        session = await _new_session(service)
        session.state["seen"] = ["a"]
        await service.append_event(session, _event("e0"))
        await service.flush()

        stored = _state_doc(db)["state"]
        session.state["seen"].append("b")

        # The stored document must not share the list with the live session.
        assert stored["seen"] == ["a"]

    @pytest.mark.asyncio
    async def test_state_that_is_not_json_clean_still_gets_stored(
        self, service, db
    ):
        class _Odd:
            def __str__(self):
                return "odd"

        session = await _new_session(service)
        session.state["thing"] = _Odd()

        await service.append_event(session, _event("e0"))
        await service.flush()

        # Failing here rather than at the Firestore call is the point.
        assert _state_doc(db)["state"]["thing"] == "odd"


class TestMigrationWritesWhereItWasRead:
    """The legacy blob carries the id it was written with.

    The write path addresses documents by `session.id`, so migrating a
    document whose stored id differs from the one being read wrote the result
    under the OTHER document. Usually they agree and nothing shows. The case
    where they differ is a copy — and a copy is exactly what you make when you
    want to try a migration without touching the original, which is how this
    was found: a verification run against real Firestore migrated the real
    session it was supposed to be leaving alone.
    """

    @pytest.mark.asyncio
    async def test_a_copy_migrates_under_its_own_id(self, service, db):
        # A session written under one id, copied to another.
        seed = FirestoreADKSessionService(project_id="test")
        original = await seed.create_session(
            app_name="agents", user_id="ana", session_id="original"
        )
        for n in range(3):
            await seed.append_event(original, _event(f"e{n}"))
        await seed.flush()

        legacy = {
            "session_id": "original",
            "app_name": "agents",
            "user_id": "ana",
            "data": original.model_dump_json(),
            "event_count": 3,
        }
        db.store.clear()
        db.store[("adk_sessions", "original")] = dict(legacy)
        db.store[("adk_sessions", "kopija")] = dict(legacy)

        fresh = FirestoreADKSessionService(project_id="test")
        restored = await fresh.get_session(
            app_name="agents", user_id="ana", session_id="kopija"
        )

        assert restored is not None
        assert restored.id == "kopija"
        # The copy is migrated...
        assert "data" not in _state_doc(db, "kopija")
        assert len(_event_docs(db, "kopija")) == 3
        # ...and the original is untouched.
        assert "data" in _state_doc(db, "original")
        assert _event_docs(db, "original") == []
