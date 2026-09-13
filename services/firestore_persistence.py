"""
Firestore Persistence Service
==============================
Async fire-and-forget persistence for WebInterface sessions, messages,
traces, audit logs, and user preferences.

Uses AsyncClient for non-blocking writes. All public write methods
return immediately - actual Firestore writes happen in background.

Collections used:
  - conversations/{session_id}              Session metadata
  - conversations/{session_id}/messages/    Individual messages (subcollection)
  - audit_log/{auto_id}                    Agent action audit trail
  - user_preferences/{user_id}             Per-user settings
"""

import os
import logging
import time
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from google.cloud.firestore_v1.async_client import AsyncClient

logger = logging.getLogger(__name__)


class FirestorePersistenceService:
    """
    Manages all Firestore persistence for the web interface.

    Design:
      - Lazy-initialized AsyncClient (created on first use)
      - All write methods are plain async (caller wraps in create_task)
      - Read methods are awaited normally (used at startup)
    """

    def __init__(self, project_id: Optional[str] = None):
        self._project_id = project_id or os.environ.get('GOOGLE_CLOUD_PROJECT')
        self._db: Optional[AsyncClient] = None

    def _get_db(self) -> AsyncClient:
        if self._db is None:
            self._db = AsyncClient(project=self._project_id)
            logger.info(f"Firestore AsyncClient initialized (project={self._project_id})")
        return self._db

    # ===================================================================
    # SESSION METADATA
    # ===================================================================

    async def save_session(
        self,
        session_id: str,
        user_id: str,
        title: str = "",
        is_active: bool = True,
    ) -> None:
        """Save or update session metadata in conversations/{session_id}."""
        try:
            db = self._get_db()
            doc_ref = db.collection("conversations").document(session_id)

            # Check if doc exists (update vs create)
            doc = await doc_ref.get()
            if doc.exists:
                await doc_ref.update({
                    "is_active": is_active,
                    "updated_at": datetime.now(timezone.utc),
                })
            else:
                await doc_ref.set({
                    "session_id": session_id,
                    "user_id": user_id,
                    "title": title,
                    "status": "active",
                    "is_active": is_active,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                    "message_count": 0,
                    "agents_used": [],
                })
            logger.debug(f"Session saved: {session_id}")
        except Exception as e:
            logger.error(f"Failed to save session {session_id}: {e}")

    async def update_session_title(self, session_id: str, title: str) -> None:
        """Update session title (auto-generated from first user message)."""
        try:
            db = self._get_db()
            await db.collection("conversations").document(session_id).update({
                "title": title[:100],
                "updated_at": datetime.now(timezone.utc),
            })
        except Exception as e:
            logger.error(f"Failed to update session title {session_id}: {e}")

    async def update_session_activity(self, session_id: str) -> None:
        """Touch updated_at timestamp and increment message count."""
        try:
            db = self._get_db()
            from google.cloud.firestore_v1 import transforms
            # set(merge=True) so it works even if the session doc doesn't exist
            # yet (fire-and-forget create may not have landed) — avoids 404.
            await db.collection("conversations").document(session_id).set({
                "updated_at": datetime.now(timezone.utc),
                "message_count": transforms.Increment(1),
            }, merge=True)
        except Exception as e:
            logger.error(f"Failed to update session activity {session_id}: {e}")

    async def deactivate_session(self, session_id: str) -> None:
        """Mark session as inactive (when user switches sessions)."""
        try:
            db = self._get_db()
            # set(merge=True) instead of update() so a not-yet-persisted session
            # doc doesn't raise "404 No document to update".
            await db.collection("conversations").document(session_id).set({
                "is_active": False,
                "updated_at": datetime.now(timezone.utc),
            }, merge=True)
        except Exception as e:
            logger.error(f"Failed to deactivate session {session_id}: {e}")

    async def load_all_sessions(self, limit: int = 100) -> Dict[str, Any]:
        """
        Load sessions from Firestore on startup.

        Returns dict with keys matching WebInterface in-memory structures:
            user_sessions: Dict[str, str]
            user_all_sessions: Dict[str, List[str]]
            session_owners: Dict[str, str]
            message_history: Dict[str, List[Dict]]
            event_traces: Dict[str, List[Dict]]
        """
        user_sessions: Dict[str, str] = {}
        user_all_sessions: Dict[str, List[str]] = {}
        session_owners: Dict[str, str] = {}
        message_history: Dict[str, List[Dict]] = {}
        event_traces: Dict[str, List[Dict]] = {}

        try:
            db = self._get_db()
            query = (
                db.collection("conversations")
                .order_by("updated_at", direction="DESCENDING")
                .limit(limit)
            )

            docs = []
            async for doc in query.stream():
                data = doc.to_dict()
                if not data or doc.id.startswith("_"):
                    continue
                docs.append((doc.id, data))

            logger.info(f"Loading {len(docs)} sessions from Firestore...")

            for session_id, data in docs:
                user_id = data.get("user_id", "unknown")

                # Rebuild ownership maps
                session_owners[session_id] = user_id
                if user_id not in user_all_sessions:
                    user_all_sessions[user_id] = []
                user_all_sessions[user_id].append(session_id)

                # Active session (first one per user, since sorted by updated_at DESC)
                if data.get("is_active", False) or user_id not in user_sessions:
                    user_sessions[user_id] = session_id

                # Load messages from subcollection
                messages = []
                msg_query = (
                    db.collection("conversations")
                    .document(session_id)
                    .collection("messages")
                    .order_by("timestamp")
                )
                async for msg_doc in msg_query.stream():
                    msg_data = msg_doc.to_dict()
                    if msg_data and not msg_doc.id.startswith("_"):
                        # Convert Firestore timestamp to float
                        ts = msg_data.get("timestamp")
                        if hasattr(ts, 'timestamp'):
                            ts = ts.timestamp()
                        elif not isinstance(ts, (int, float)):
                            ts = time.time()
                        messages.append({
                            "role": msg_data.get("role", "user"),
                            "content": msg_data.get("content", ""),
                            "timestamp": ts,
                        })
                message_history[session_id] = messages

                # Load traces (stored as array field on session doc)
                traces = data.get("traces", [])
                event_traces[session_id] = traces if isinstance(traces, list) else []

            logger.info(
                f"Loaded {len(session_owners)} sessions, "
                f"{sum(len(m) for m in message_history.values())} messages"
            )

        except Exception as e:
            logger.error(f"Failed to load sessions from Firestore: {e}")

        return {
            "user_sessions": user_sessions,
            "user_all_sessions": user_all_sessions,
            "session_owners": session_owners,
            "message_history": message_history,
            "event_traces": event_traces,
        }

    # ===================================================================
    # MESSAGES
    # ===================================================================

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: float,
        agent_name: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
    ) -> None:
        """Save a message to conversations/{session_id}/messages/."""
        try:
            db = self._get_db()
            msg_data = {
                "role": role,
                "content": content,
                "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
                "agent_name": agent_name,
                "media_urls": media_urls or [],
            }
            await (
                db.collection("conversations")
                .document(session_id)
                .collection("messages")
                .add(msg_data)
            )
            # Update session activity
            await self.update_session_activity(session_id)
            logger.debug(f"Message saved: {session_id} ({role})")
        except Exception as e:
            logger.error(f"Failed to save message for {session_id}: {e}")

    # ===================================================================
    # TRACE EVENTS
    # ===================================================================

    async def save_trace_events(
        self,
        session_id: str,
        events: List[Dict],
    ) -> None:
        """Append trace events to conversations/{session_id} traces array."""
        if not events:
            return
        try:
            db = self._get_db()
            from google.cloud.firestore_v1 import transforms
            await db.collection("conversations").document(session_id).update({
                "traces": transforms.ArrayUnion(events),
                "updated_at": datetime.now(timezone.utc),
            })
            logger.debug(f"Saved {len(events)} trace events for {session_id}")
        except Exception as e:
            logger.error(f"Failed to save traces for {session_id}: {e}")

    # ===================================================================
    # AUDIT LOG
    # ===================================================================

    async def log_audit(
        self,
        session_id: str,
        user_id: str,
        agent_name: str,
        action_type: str,
        action_description: str = "",
        target_resource: str = "",
        target_service: str = "",
        status: str = "success",
        details: Optional[Dict] = None,
    ) -> None:
        """Write an entry to the audit_log collection."""
        try:
            db = self._get_db()
            await db.collection("audit_log").add({
                "timestamp": datetime.now(timezone.utc),
                "session_id": session_id,
                "user_id": user_id,
                "agent_name": agent_name,
                "action_type": action_type,
                "action_description": action_description,
                "target_resource": target_resource,
                "target_service": target_service,
                "status": status,
                "metadata": details or {},
            })
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    # ===================================================================
    # USER PREFERENCES
    # ===================================================================

    async def load_user_preferences(self, user_id: str) -> Dict[str, Any]:
        """Load user preferences from user_preferences/{user_id}."""
        try:
            db = self._get_db()
            doc = await db.collection("user_preferences").document(user_id).get()
            if doc.exists:
                data = doc.to_dict()
                # Remove schema fields
                return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.error(f"Failed to load preferences for {user_id}: {e}")
        return {}

    async def save_user_preferences(
        self, user_id: str, prefs: Dict[str, Any]
    ) -> None:
        """Save user preferences to user_preferences/{user_id}."""
        try:
            db = self._get_db()
            prefs["updated_at"] = datetime.now(timezone.utc)
            await db.collection("user_preferences").document(user_id).set(
                prefs, merge=True
            )
        except Exception as e:
            logger.error(f"Failed to save preferences for {user_id}: {e}")

    # ===================================================================
    # CLEANUP
    # ===================================================================

    async def close(self) -> None:
        """Close the AsyncClient connection."""
        if self._db is not None:
            self._db.close()
            self._db = None
            logger.info("Firestore AsyncClient closed")
