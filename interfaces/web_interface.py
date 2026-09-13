"""
Web Interface for Google Workspace ADK System

Provides FastAPI-based web UI and REST API with:
- Non-streaming and SSE streaming chat
- Per-user session management
- Message history and agent trace tracking
- Firestore persistence (write-through cache)
"""

import logging
import asyncio
import os
import time
from typing import Optional, Dict, List, Any, AsyncGenerator

from .base_interface import BaseInterface
from config.deployment_config import is_web_scheduler_enabled

logger = logging.getLogger(__name__)


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine as a background task. Never blocks caller."""
    try:
        task = asyncio.create_task(coro)
        task.add_done_callback(_on_persist_done)
    except RuntimeError:
        # No event loop running (e.g., during tests)
        logger.debug("No event loop for fire-and-forget persistence")


def _on_persist_done(task: asyncio.Task) -> None:
    """Log errors from background persistence tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Background persistence error: {exc}")


class WebInterface(BaseInterface):
    """
    Web interface extending BaseInterface.

    Manages per-user sessions, message history,
    and event traces for the operational dashboard.
    """

    def __init__(self):
        super().__init__(session_prefix="web")

        # user_id -> current active session_id
        self.user_sessions: Dict[str, str] = {}

        # user_id -> list of all session_ids (preserves history)
        self.user_all_sessions: Dict[str, List[str]] = {}

        # session_id -> user_id (reverse lookup)
        self.session_owners: Dict[str, str] = {}

        # session_id -> list of {role, content, timestamp}
        self.message_history: Dict[str, List[Dict]] = {}

        # session_id -> list of trace events (tool calls, agent delegations)
        self.event_traces: Dict[str, List[Dict]] = {}

        # Per-session processing locks (prevents concurrent mutation)
        self.processing_locks: Dict[str, asyncio.Lock] = {}

        # Firestore persistence (lazy-initialized)
        self._persistence = None

        # Shared ADK session service (lazy-initialized).
        # One instance shared across all RunnerHelper recreations so that
        # switching sessions doesn't lose ADK conversation context.
        self._adk_session_service = None

        logger.info("WebInterface initialized")

    def _get_persistence(self):
        """Lazy-init Firestore persistence service."""
        if self._persistence is None:
            from services.firestore_persistence import FirestorePersistenceService
            self._persistence = FirestorePersistenceService()
        return self._persistence

    def _get_adk_session_service(self):
        """
        Lazy-init shared ADK session service.

        Returns FirestoreADKSessionService when USE_PERSISTENT_ADK_SESSIONS=true,
        otherwise InMemorySessionService. The same instance is reused for all
        RunnerHelper creations so session state is preserved across session switches.
        """
        if self._adk_session_service is None:
            import os
            use_persistent = os.environ.get("USE_PERSISTENT_ADK_SESSIONS", "false").lower() == "true"
            if use_persistent:
                from services.adk_session_service import FirestoreADKSessionService
                self._adk_session_service = FirestoreADKSessionService()
                logger.info("WebInterface: using FirestoreADKSessionService (shared)")
            else:
                from google.adk.sessions import InMemorySessionService
                self._adk_session_service = InMemorySessionService()
                logger.info("WebInterface: using InMemorySessionService (shared)")
        return self._adk_session_service

    def _session_service_for_runner(self):
        """Web binds every RunnerHelper to the one shared ADK session service.

        Streaming already did this; chat() kept whatever the previous helper
        carried, so the two paths could end up on different stores for the
        same conversation.
        """
        return self._get_adk_session_service()

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create processing lock for a session."""
        if session_id not in self.processing_locks:
            self.processing_locks[session_id] = asyncio.Lock()
        return self.processing_locks[session_id]

    def _register_session(self, user_id: str, session_id: str) -> None:
        """Register a session in all tracking structures."""
        # Deactivate previous session in Firestore
        old_session = self.user_sessions.get(user_id)
        if old_session and old_session != session_id:
            _fire_and_forget(
                self._get_persistence().deactivate_session(old_session)
            )

        self.user_sessions[user_id] = session_id
        self.session_owners[session_id] = user_id
        if user_id not in self.user_all_sessions:
            self.user_all_sessions[user_id] = []
        if session_id not in self.user_all_sessions[user_id]:
            self.user_all_sessions[user_id].append(session_id)
        if session_id not in self.message_history:
            self.message_history[session_id] = []
        if session_id not in self.event_traces:
            self.event_traces[session_id] = []

        # Persist new session to Firestore
        _fire_and_forget(
            self._get_persistence().save_session(
                session_id=session_id,
                user_id=user_id,
                is_active=True,
            )
        )

    def get_or_create_session(self, user_id: str) -> str:
        """Get existing active session or create new one for user."""
        if user_id not in self.user_sessions:
            session_id = self.generate_session_id(user_id)
            self._register_session(user_id, session_id)
        return self.user_sessions[user_id]

    def create_new_session(self, user_id: str) -> str:
        """Create a new session for user (keeps old sessions accessible)."""
        session_id = self.generate_session_id(user_id)
        self._register_session(user_id, session_id)
        return session_id

    def switch_session(self, user_id: str, session_id: str) -> bool:
        """Switch user's active session to an existing one."""
        if session_id in self.message_history:
            self.user_sessions[user_id] = session_id
            return True
        return False

    def list_sessions(self) -> List[Dict]:
        """List ALL sessions (not just active ones)."""
        sessions = []
        for session_id, user_id in self.session_owners.items():
            history = self.message_history.get(session_id, [])
            is_active = self.user_sessions.get(user_id) == session_id
            sessions.append({
                "user_id": user_id,
                "session_id": session_id,
                "message_count": len(history),
                "last_activity": history[-1]["timestamp"] if history else None,
                "active": is_active
            })
        # Sort: active first, then by last_activity descending
        sessions.sort(key=lambda s: (
            not s["active"],
            -(s["last_activity"] or 0)
        ))
        return sessions

    async def chat(
        self,
        user_id: str,
        message: str,
        route_hint: Optional[str] = None,
        response_mode: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a chat message (non-streaming).
        Returns dict with response text, session_id, trace.
        """
        # Honour a client-pinned session. The web UI also calls
        # /api/sessions/{id}/switch, but direct API clients only send the
        # ChatRequest field; an unknown id falls back to the active session.
        if session_id and not self.switch_session(user_id, session_id):
            logger.warning(
                "Unknown session_id %r from %s — using active session",
                session_id, user_id,
            )
        session_id = self.get_or_create_session(user_id)
        lock = await self._get_lock(session_id)

        # A second message on a session that is already working used to wait
        # here until the client gave up. Tell the user instead.
        if not await self.acquire_or_busy(lock, session_id):
            return {
                "response": self.busy_notice(session_id),
                "session_id": session_id,
                "timestamp": time.time(),
            }
        try:
            self._note_turn_start(session_id, message)
            return await self._chat_locked(
                user_id, message, session_id, route_hint, response_mode
            )
        finally:
            self._note_turn_end(session_id)
            lock.release()

    async def _chat_locked(
        self,
        user_id: str,
        message: str,
        session_id: str,
        route_hint: Optional[str] = None,
        response_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One non-streaming turn, with this session's lock ALREADY held.

        chat_stream's CLASSROOM fallback used to call chat() while holding
        that very lock. asyncio.Lock is not reentrant, so the request blocked
        on itself and never released it — the session stayed wedged until a
        restart. Both callers now share this body and each takes the lock
        exactly once.
        """
        persistence = self._get_persistence()

        # Record user message
        user_ts = time.time()
        self.message_history[session_id].append({
            "role": "user",
            "content": message,
            "timestamp": user_ts
        })
        _fire_and_forget(persistence.save_message(
            session_id=session_id, role="user",
            content=message, timestamp=user_ts,
        ))

        # Auto-set session title from first message
        if len(self.message_history[session_id]) == 1:
            title = message[:100].strip()
            _fire_and_forget(
                persistence.update_session_title(session_id, title)
            )

        # Process through BaseInterface.process_message
        response = await self.process_message(
            user_id=user_id,
            message=message,
            session_id=session_id,
            route_hint=route_hint,
            response_mode=response_mode,
        )

        # Record assistant message
        timestamp = time.time()
        self.message_history[session_id].append({
            "role": "assistant",
            "content": response,
            "timestamp": timestamp
        })
        _fire_and_forget(persistence.save_message(
            session_id=session_id, role="assistant",
            content=response, timestamp=timestamp,
        ))

        # Audit log
        _fire_and_forget(persistence.log_audit(
            session_id=session_id, user_id=user_id,
            agent_name="orchestrator", action_type="chat_response",
            action_description=f"Response to: {message[:80]}",
        ))

        return {
            "response": response,
            "session_id": session_id,
            "timestamp": timestamp
        }

    async def chat_stream(
        self, user_id: str, message: str,
        attachments: list = None,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Process a chat message with streaming via orchestrator_helper.stream().
        Yields SSE-compatible event dicts with types: text, tool_call, tool_response, done.
        """
        if session_id and not self.switch_session(user_id, session_id):
            logger.warning(
                "Unknown session_id %r from %s — using active session",
                session_id, user_id,
            )
        session_id = self.get_or_create_session(user_id)
        lock = await self._get_lock(session_id)

        # Same as chat(): say the session is busy rather than hang on the lock.
        if not await self.acquire_or_busy(lock, session_id):
            yield {"event": "text", "data": self.busy_notice(session_id),
                   "author": "system"}
            yield {"event": "done", "data": {"session_id": session_id}}
            return

        try:
            self._note_turn_start(session_id, message)
            # Ensure system initialized
            if self.system is None:
                self.initialize_system()

            # CLASSROOM mode doesn't support streaming - fallback.
            # _chat_locked, not chat(): we already hold this session's lock.
            # It runs process_message, which prepares the turn itself, so this
            # branch must come BEFORE _prepare_turn — arming the same turn
            # twice would consume the user's "da" before the tool sees it.
            if self.system.active_mode == "CLASSROOM":
                result = await self._chat_locked(user_id, message, session_id)
                yield {"event": "text", "data": result["response"], "author": "socrates"}
                yield {"event": "done", "data": {"session_id": session_id}}
                return

            # Same preparation as the non-streaming path: session binding,
            # approval arming, runner binding. Streaming used to do its own
            # half of this and skip the approvals half, so nothing ever armed
            # and a "da" in the web UI could not authorise anything.
            ctx = await self._prepare_turn(user_id, message, session_id)


            # Record user message
            user_ts = time.time()
            self.message_history[session_id].append({
                "role": "user",
                "content": message,
                "timestamp": user_ts
            })
            persistence = self._get_persistence()
            _fire_and_forget(persistence.save_message(
                session_id=session_id, role="user",
                content=message, timestamp=user_ts,
            ))

            # Auto-set session title from first message
            if len(self.message_history[session_id]) == 1:
                title = message[:100].strip()
                _fire_and_forget(
                    persistence.update_session_title(session_id, title)
                )

            # Explicit mode commands belong to every door, not only the
            # non-streaming one — /classroom typed in the browser used to go
            # to the runner as an ordinary message. Placed after _prepare_turn
            # so the turn is still prepared exactly once, and after the user
            # message is recorded so the history reads straight.
            explicit_mode_reply = self._handle_classroom_command(message)
            if explicit_mode_reply is not None:
                reply_ts = time.time()
                self.message_history[session_id].append({
                    "role": "assistant",
                    "content": explicit_mode_reply,
                    "timestamp": reply_ts,
                })
                _fire_and_forget(persistence.save_message(
                    session_id=session_id, role="assistant",
                    content=explicit_mode_reply, timestamp=reply_ts,
                ))
                yield {"event": "text", "data": explicit_mode_reply, "author": "system"}
                yield {"event": "done", "data": {"session_id": session_id}}
                return

            # The streaming path drives the runner directly, so it does not
            # pass through _orchestrate and needs the same check: opening this
            # conversation in the browser while a voice job is still writing
            # to it would interleave the two.
            busy = self.session_busy_answer(session_id)
            if busy is not None:
                reply_ts = time.time()
                self.message_history[session_id].append({
                    "role": "assistant",
                    "content": busy,
                    "timestamp": reply_ts,
                })
                _fire_and_forget(persistence.save_message(
                    session_id=session_id, role="assistant",
                    content=busy, timestamp=reply_ts,
                ))
                yield {"event": "text", "data": busy, "author": "system"}
                yield {"event": "done", "data": {"session_id": session_id}}
                return

            # USE_PLAN_EXECUTE only ever applied to the non-streaming path,
            # so the same request was decomposed in HA Assist and run in one
            # pass in the browser. Plan-execute has no token stream to forward
            # — each step runs in its own isolated session — so it is
            # delivered as one message, with a note first because it takes a
            # while.
            # Attachments stay on the streaming path. Plan-execute takes a
            # plain string, so routing an image here would drop it silently —
            # "analiziraj priloženu sliku pa napravi dokument" would arrive as
            # text alone. Better one request handled the old way than a
            # request quietly missing half its input.
            if attachments:
                logger.info(
                    "Plan-execute skipped for a request with %d attachment(s)",
                    len(attachments),
                )
            if not attachments and self._plan_execute_applies(message):
                yield {
                    "event": "text",
                    "data": os.getenv(
                        "PLAN_EXECUTE_ACK_TEXT",
                        "Ovo ima više koraka — radim ih redom, javim kad završim.",
                    ) + "\n\n",
                    "author": "orchestrator",
                }
                planned = await self._orchestrate(ctx)(message)
                reply_ts = time.time()
                self.message_history[session_id].append({
                    "role": "assistant",
                    "content": planned,
                    "timestamp": reply_ts,
                })
                _fire_and_forget(persistence.save_message(
                    session_id=session_id, role="assistant",
                    content=planned, timestamp=reply_ts,
                ))
                yield {"event": "text", "data": planned, "author": "orchestrator"}
                yield {"event": "done", "data": {"session_id": session_id}}
                return

            full_text = ""
            trace_events = []

            # Build multimodal content once (reused across retry attempts)
            _multimodal_content = None
            if attachments:
                from google.genai import types
                import base64 as b64mod
                parts = []
                for att in attachments:
                    if att.get("base64"):
                        img_bytes = b64mod.b64decode(att["base64"])
                        parts.append(types.Part.from_bytes(
                            data=img_bytes,
                            mime_type=att.get("mime_type", "image/jpeg")
                        ))
                parts.append(types.Part(text=message))
                _multimodal_content = types.Content(role="user", parts=parts)

            _RETRY_DELAYS = [30, 60]  # seconds to wait before attempt 2 and 3
            # Once ANY tool/worker has been invoked, the run has side effects
            # (e.g. generate_visual_asset already spent tokens / created media),
            # so we must NOT retry the whole workflow — that would duplicate the
            # generation and double-charge. Only a pure pre-tool LLM 429 is safe
            # to retry.
            _tool_called = False

            for _attempt in range(len(_RETRY_DELAYS) + 1):
                try:
                    if _multimodal_content is not None:
                        event_stream = ctx.helper.stream_content(_multimodal_content)
                    else:
                        event_stream = ctx.helper.stream(message)

                    async for event in event_stream:
                        author = getattr(event, 'author', 'unknown')

                        if not (hasattr(event, 'content') and event.content
                                and hasattr(event.content, 'parts') and event.content.parts):
                            continue

                        for part in event.content.parts:
                            # Text chunk
                            if hasattr(part, 'text') and part.text:
                                full_text += part.text
                                yield {
                                    "event": "text",
                                    "data": part.text,
                                    "author": author
                                }

                            # Inline image data (from Gemini native image generation)
                            if hasattr(part, 'inline_data') and part.inline_data:
                                try:
                                    import base64 as b64mod
                                    from services.media_service import save_generated_image
                                    img_bytes = part.inline_data.data
                                    mime = getattr(part.inline_data, 'mime_type', 'image/png')
                                    saved = save_generated_image(img_bytes, prefix="agent")
                                    img_tag = f"\n[IMAGE:{saved['url_path']}:Generated by agent]\n"
                                    full_text += img_tag
                                    yield {
                                        "event": "image",
                                        "data": {
                                            "url": saved["url_path"],
                                            "file_id": saved["file_id"],
                                            "alt": "Generated by agent",
                                        },
                                        "author": author
                                    }
                                except Exception as img_err:
                                    logger.error(f"Failed to save inline image: {img_err}")

                            # Tool call
                            if hasattr(part, 'function_call') and part.function_call:
                                fc = part.function_call
                                _tool_called = True  # run now has side effects — do not retry
                                trace_entry = {
                                    "type": "tool_call",
                                    "name": getattr(fc, 'name', 'unknown'),
                                    "args": str(getattr(fc, 'args', {}))[:500],
                                    "author": author,
                                    "timestamp": time.time()
                                }
                                trace_events.append(trace_entry)
                                yield {"event": "tool_call", "data": trace_entry}

                            # Tool response
                            if hasattr(part, 'function_response') and part.function_response:
                                fr = part.function_response
                                resp_data = getattr(fr, 'response', None)
                                trace_entry = {
                                    "type": "tool_response",
                                    "name": getattr(fr, 'name', 'unknown'),
                                    "result": str(resp_data)[:1000] if resp_data else "",
                                    "author": author,
                                    "timestamp": time.time()
                                }
                                trace_events.append(trace_entry)
                                yield {"event": "tool_response", "data": trace_entry}

                                # Check if tool response contains image URLs
                                logger.debug(f"Tool response '{getattr(fr, 'name', '?')}': type={type(resp_data)}, data={str(resp_data)[:300]}")
                                if isinstance(resp_data, dict):
                                    # Direct dict with preview_url (e.g., generate_visual_asset)
                                    inner = resp_data.get("result", resp_data) if isinstance(resp_data.get("result"), dict) else resp_data
                                    preview_url = inner.get("preview_url") or inner.get("public_url") or inner.get("local_url")
                                    status = inner.get("status", "")
                                    asset_type = inner.get("asset_type", "IMAGE").upper()
                                    if preview_url and status == "success":
                                        tag_type = "VIDEO" if asset_type == "VIDEO" else "IMAGE"
                                        alt_text = inner.get("prompt", "Generated asset")[:80]
                                        media_tag = f"\n[{tag_type}:{preview_url}:{alt_text}]\n"
                                        # Add to full_text so it persists in history
                                        if media_tag not in full_text:
                                            full_text += media_tag
                                        yield {
                                            "event": "image",
                                            "data": {
                                                "url": preview_url,
                                                "alt": alt_text,
                                                "type": "video" if asset_type == "VIDEO" else "image",
                                            },
                                            "author": author
                                        }

                                    # Also check string results for [IMAGE:...] and [VIDEO:...] tags
                                    # (sub-agent text responses come as {"result": "text..."})
                                    result_str = resp_data.get("result", "")
                                    if isinstance(result_str, str):
                                        import re
                                        if "[IMAGE:" in result_str:
                                            img_matches = re.findall(
                                                r'\[IMAGE:(/api/media/[^\]:]+):([^\]]*)\]',
                                                result_str
                                            )
                                            for img_url, img_alt in img_matches:
                                                tag = f"\n[IMAGE:{img_url}:{img_alt}]\n"
                                                if tag not in full_text:
                                                    full_text += tag
                                                yield {
                                                    "event": "image",
                                                    "data": {
                                                        "url": img_url,
                                                        "alt": img_alt or "Generated image",
                                                    },
                                                    "author": author
                                                }
                                        if "[VIDEO:" in result_str:
                                            vid_matches = re.findall(
                                                r'\[VIDEO:(/api/media/[^\]:]+):([^\]]*)\]',
                                                result_str
                                            )
                                            for vid_url, vid_alt in vid_matches:
                                                tag = f"\n[VIDEO:{vid_url}:{vid_alt}]\n"
                                                if tag not in full_text:
                                                    full_text += tag
                                                yield {
                                                    "event": "image",
                                                    "data": {
                                                        "url": vid_url,
                                                        "alt": vid_alt or "Generated video",
                                                        "type": "video",
                                                    },
                                                    "author": author
                                                }

                    break  # stream completed successfully — exit retry loop

                except Exception as e:
                    err_str = str(e)
                    is_quota = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str

                    if is_quota and _attempt < len(_RETRY_DELAYS) and not full_text and not _tool_called:
                        wait = _RETRY_DELAYS[_attempt]
                        logger.warning(
                            f"Vertex AI 429 quota error (attempt {_attempt+1}/{len(_RETRY_DELAYS)+1}), "
                            f"retrying in {wait}s: {err_str[:120]}"
                        )
                        yield {
                            "event": "text",
                            "data": (
                                f"\n⏳ API kvota privremeno iscrpljena. "
                                f"Čekam {wait}s i pokušavam ponovo "
                                f"(pokušaj {_attempt+2}/{len(_RETRY_DELAYS)+1})...\n"
                            )
                        }
                        await asyncio.sleep(wait)
                        continue  # retry

                    elif is_quota and full_text:
                        # Response partially delivered — inform user
                        logger.warning(f"Vertex AI 429 mid-stream (attempt {_attempt+1}): {err_str[:120]}")
                        yield {
                            "event": "text",
                            "data": (
                                "\n\n⚠️ Odgovor je prekinut zbog API kvote (429). "
                                "Pošalji poruku ponovo za nastavak."
                            )
                        }
                        break

                    else:
                        logger.error(f"Stream error (attempt {_attempt+1}): {e}")
                        yield {"event": "error", "data": err_str}
                        break

            # Store results
            assistant_ts = time.time()
            self.message_history[session_id].append({
                "role": "assistant",
                "content": full_text,
                "timestamp": assistant_ts
            })
            self.event_traces[session_id].extend(trace_events)

            # Persist to Firestore (fire-and-forget)
            _fire_and_forget(persistence.save_message(
                session_id=session_id, role="assistant",
                content=full_text, timestamp=assistant_ts,
            ))
            if trace_events:
                _fire_and_forget(persistence.save_trace_events(
                    session_id=session_id, events=trace_events,
                ))
            _fire_and_forget(persistence.log_audit(
                session_id=session_id, user_id=user_id,
                agent_name="orchestrator", action_type="chat_stream_response",
                action_description=f"Streamed response to: {message[:80]}",
            ))

            yield {"event": "done", "data": {"session_id": session_id}}
        finally:
            self._note_turn_end(session_id)
            lock.release()

    def get_trace(self, session_id: str) -> List[Dict]:
        """Get event trace for a session."""
        return self.event_traces.get(session_id, [])

    def get_history(self, session_id: str) -> List[Dict]:
        """Get message history for a session."""
        return self.message_history.get(session_id, [])

    def format_response(self, response: str) -> str:
        """Web interface returns response as-is."""
        return response

    async def start(self) -> None:
        """Initialize the agent system, start scheduler, and load persisted sessions."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.initialize_system)

        # Start scheduler for recurring jobs only when explicitly enabled.
        # On rpi-home we keep scheduler out of the web process by default.
        if is_web_scheduler_enabled():
            try:
                from interfaces.scheduler_interface import SchedulerInterface
                from tools.adk_tools.scheduler_adk_tools import set_scheduler_instance
                self.system.scheduler = SchedulerInterface()
                self.system.scheduler.system = self.system
                self.system.scheduler._load_saved_jobs()
                self.system.scheduler.scheduler.start()
                set_scheduler_instance(self.system.scheduler)
                job_count = len(self.system.scheduler.config.jobs)
                logger.info(f"Scheduler started with {job_count} jobs")
            except Exception as e:
                logger.error(f"Failed to start scheduler: {e}")
        else:
            logger.info("Embedded web scheduler disabled (ENABLE_WEB_SCHEDULER=false)")

        # Load sessions from Firestore
        try:
            persistence = self._get_persistence()
            data = await persistence.load_all_sessions(limit=100)
            self.user_sessions.update(data["user_sessions"])
            self.user_all_sessions.update(data["user_all_sessions"])
            self.session_owners.update(data["session_owners"])
            self.message_history.update(data["message_history"])
            self.event_traces.update(data["event_traces"])
            logger.info(
                f"Restored {len(data['session_owners'])} sessions from Firestore "
                f"({sum(len(m) for m in data['message_history'].values())} messages)"
            )
        except Exception as e:
            logger.error(f"Failed to load sessions from Firestore: {e}")
            logger.info("Continuing with empty session state")

        logger.info("WebInterface system initialized")

    async def stop(self) -> None:
        """Cleanup persistence connections.

        Two different stores, and only the first was ever closed: the ADK
        session service writes conversation history in the background, and
        its close() had no callers at all — so a shutdown dropped whatever
        was still in flight and the history came back short.
        """
        if self._persistence is not None:
            await self._persistence.close()

        if self._adk_session_service is not None:
            closer = getattr(self._adk_session_service, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    logger.warning("ADK session service close failed", exc_info=True)

        logger.info("WebInterface stopped")
