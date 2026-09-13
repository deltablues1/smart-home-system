"""
HITL Firestore Service
======================
Stores HITL (Human-in-the-Loop) approval requests in Firestore so that
non-terminal interfaces (web, scheduler) can surface them to a human
without blocking with input().

Collection: pending_approvals/{confirmation_id}

Flow:
  1. Orchestrator calls create_pending() → writes to Firestore
  2. Orchestrator calls wait_for_decision() → polls Firestore
  3. Dashboard polls GET /api/hitl/pending → shows banner
  4. User clicks Approve/Reject → POST /api/hitl/{id}/approve|reject
  5. Web endpoint calls approve()/reject() → updates Firestore
  6. Orchestrator polling detects decision → continues execution
"""

import os
import asyncio
import logging
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from google.cloud.firestore_v1.async_client import AsyncClient

logger = logging.getLogger(__name__)

_COLLECTION = "pending_approvals"
_DEFAULT_TIMEOUT = 300      # 5 minutes
_DEFAULT_POLL_INTERVAL = 2  # seconds


class HITLFirestoreService:
    """
    Manages HITL approval requests in Firestore.

    Designed for use by:
      - long-running agent flows: create + wait
      - web/app.py endpoints: approve / reject / list
    """

    def __init__(self, project_id: Optional[str] = None):
        self._project_id = project_id or os.environ.get('GOOGLE_CLOUD_PROJECT')
        self._db: Optional[AsyncClient] = None

    def _get_db(self) -> AsyncClient:
        if self._db is None:
            self._db = AsyncClient(project=self._project_id)
            logger.info(f"[HITLService] Firestore client initialized (project={self._project_id})")
        return self._db

    # ------------------------------------------------------------------
    # Write operations (called by orchestrator)
    # ------------------------------------------------------------------

    async def create_pending(
        self,
        confirmation_id: str,
        session_id: str,
        user_id: str,
        display_text: str,
        invoice_summary: Dict[str, Any],
        has_warnings: bool = False,
    ) -> str:
        """
        Create a pending approval request in Firestore.
        Returns confirmation_id (same as input, for convenience).
        """
        try:
            db = self._get_db()
            await db.collection(_COLLECTION).document(confirmation_id).set({
                "confirmation_id": confirmation_id,
                "session_id": session_id,
                "user_id": user_id,
                "display_text": display_text,
                "invoice_summary": invoice_summary,
                "has_warnings": has_warnings,
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
                "decided_at": None,
                "rejection_reason": None,
            })
            logger.info(f"[HITLService] Created pending approval '{confirmation_id}' for session '{session_id}'")
        except Exception as e:
            logger.error(f"[HITLService] Failed to create pending '{confirmation_id}': {e}")
            raise
        return confirmation_id

    async def wait_for_decision(
        self,
        confirmation_id: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> Dict[str, Any]:
        """
        Poll Firestore until the confirmation is approved/rejected or times out.

        Returns dict:
          {"status": "approved" | "rejected" | "timeout", "reason": str | None}
        """
        db = self._get_db()
        doc_ref = db.collection(_COLLECTION).document(confirmation_id)
        elapsed = 0.0

        logger.info(
            f"[HITLService] Waiting for decision on '{confirmation_id}' "
            f"(timeout={timeout_seconds}s, poll={poll_interval}s)"
        )

        while elapsed < timeout_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                doc = await doc_ref.get()
                if not doc.exists:
                    logger.warning(f"[HITLService] Document '{confirmation_id}' disappeared during polling")
                    return {"status": "rejected", "reason": "Approval request not found"}

                data = doc.to_dict()
                status = data.get("status", "pending")

                if status == "approved":
                    logger.info(f"[HITLService] '{confirmation_id}' APPROVED after {elapsed:.0f}s")
                    return {"status": "approved", "reason": None}

                if status == "rejected":
                    reason = data.get("rejection_reason") or "User rejected"
                    logger.info(f"[HITLService] '{confirmation_id}' REJECTED after {elapsed:.0f}s: {reason}")
                    return {"status": "rejected", "reason": reason}

            except Exception as e:
                logger.error(f"[HITLService] Poll error for '{confirmation_id}': {e}")
                # Continue polling despite transient errors

        logger.warning(f"[HITLService] '{confirmation_id}' timed out after {timeout_seconds}s")
        # Mark as expired in Firestore
        try:
            await doc_ref.update({
                "status": "expired",
                "decided_at": datetime.now(timezone.utc),
            })
        except Exception:
            pass
        return {"status": "timeout", "reason": f"No decision within {timeout_seconds}s"}

    # ------------------------------------------------------------------
    # Decision operations (called by web API endpoints)
    # ------------------------------------------------------------------

    async def approve(self, confirmation_id: str) -> bool:
        """Mark confirmation as approved. Returns True on success."""
        try:
            db = self._get_db()
            doc_ref = db.collection(_COLLECTION).document(confirmation_id)
            doc = await doc_ref.get()
            if not doc.exists:
                return False
            data = doc.to_dict()
            if data.get("status") != "pending":
                return False
            await doc_ref.update({
                "status": "approved",
                "decided_at": datetime.now(timezone.utc),
            })
            logger.info(f"[HITLService] Approved '{confirmation_id}'")
            return True
        except Exception as e:
            logger.error(f"[HITLService] Failed to approve '{confirmation_id}': {e}")
            return False

    async def reject(self, confirmation_id: str, reason: str = "") -> bool:
        """Mark confirmation as rejected. Returns True on success."""
        try:
            db = self._get_db()
            doc_ref = db.collection(_COLLECTION).document(confirmation_id)
            doc = await doc_ref.get()
            if not doc.exists:
                return False
            data = doc.to_dict()
            if data.get("status") != "pending":
                return False
            await doc_ref.update({
                "status": "rejected",
                "decided_at": datetime.now(timezone.utc),
                "rejection_reason": reason or "User rejected",
            })
            logger.info(f"[HITLService] Rejected '{confirmation_id}': {reason}")
            return True
        except Exception as e:
            logger.error(f"[HITLService] Failed to reject '{confirmation_id}': {e}")
            return False

    # ------------------------------------------------------------------
    # Read operations (called by web API endpoints)
    # ------------------------------------------------------------------

    async def list_pending(self) -> List[Dict[str, Any]]:
        """Return all currently pending approvals, ordered by created_at ascending.

        Uses a simple collection scan filtered in-memory to avoid requiring
        a Firestore composite index on (status, created_at).
        """
        try:
            db = self._get_db()
            results = []
            async for doc in db.collection(_COLLECTION).stream():
                data = doc.to_dict()
                if not data or data.get("status") != "pending":
                    continue
                # Convert Firestore timestamps to ISO strings for JSON serialization
                for field in ("created_at", "decided_at"):
                    val = data.get(field)
                    if hasattr(val, "isoformat"):
                        data[field] = val.isoformat()
                results.append(data)
            # Sort in memory — collection is tiny (≤ handful of pending approvals)
            results.sort(key=lambda x: x.get("created_at") or "")
            return results
        except Exception as e:
            logger.error(f"[HITLService] Failed to list pending: {e}")
            return []

    async def get_one(self, confirmation_id: str) -> Optional[Dict[str, Any]]:
        """Return a single confirmation document (any status)."""
        try:
            db = self._get_db()
            doc = await db.collection(_COLLECTION).document(confirmation_id).get()
            if not doc.exists:
                return None
            data = doc.to_dict()
            for field in ("created_at", "decided_at"):
                val = data.get(field)
                if hasattr(val, "isoformat"):
                    data[field] = val.isoformat()
            return data
        except Exception as e:
            logger.error(f"[HITLService] Failed to get '{confirmation_id}': {e}")
            return None

    async def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


# ---------------------------------------------------------------------------
# Module-level singleton (used by web/app.py endpoints)
# ---------------------------------------------------------------------------

_hitl_service: Optional[HITLFirestoreService] = None


def get_hitl_firestore_service() -> HITLFirestoreService:
    """Return the module-level singleton (lazy init)."""
    global _hitl_service
    if _hitl_service is None:
        _hitl_service = HITLFirestoreService()
    return _hitl_service


def generate_confirmation_id(session_id: str = "") -> str:
    """Generate a unique confirmation ID."""
    short = uuid.uuid4().hex[:8]
    return f"hitl-{short}"
