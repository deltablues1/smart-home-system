"""
Telegram Interface for Google Workspace ADK System

Provides Telegram bot integration with support for both:
- Polling mode (for local development)
- Webhook mode (for Cloud Run deployment)
"""

import os
import logging
import asyncio
import base64
import html
import json
import tempfile
from typing import Optional, Set
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode, ChatAction

from .base_interface import BaseInterface
from config.deployment_config import is_telegram_enabled
from utils.process_guard import (
    notify_ready,
    notify_watchdog,
    watchdog_interval_seconds,
)
from services.audio_ingress import (
    AudioIngressError,
    get_audio_ingress_service,
    get_supported_audio_mime_types,
)

logger = logging.getLogger(__name__)

# Configuration from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")

class PollingUnhealthy(RuntimeError):
    """Raised when the bot is still a process but has stopped being a bot."""


# Telegram message limit
MAX_MESSAGE_LENGTH = 4096
MAX_AUDIO_DURATION_SECONDS = int(os.getenv("TELEGRAM_AUDIO_MAX_DURATION_SECONDS", "120"))
# Input guards: cap LLM cost per message and photo download size
MAX_INPUT_TEXT_LENGTH = int(os.getenv("TELEGRAM_MAX_INPUT_TEXT_LENGTH", "4000"))
MAX_PHOTO_BYTES = int(os.getenv("TELEGRAM_MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))


def authorized_only(func):
    """Decorator to restrict access to authorized chat IDs only."""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)

        # Get list of authorized chat IDs
        authorized_ids = self._get_authorized_chat_ids()

        if chat_id not in authorized_ids:
            logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
            await update.message.reply_text(
                "Unauthorized. This bot is private.\n"
                f"Your chat ID: `{chat_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        return await func(self, update, context)
    return wrapper


class TelegramInterface(BaseInterface):
    """
    Telegram bot interface for the Google Workspace ADK System.

    Features:
    - Same agent system as CLI (main.py)
    - Persistent sessions per chat
    - Both polling and webhook support
    - Markdown formatting
    - Long message handling
    """

    def __init__(self):
        """Initialize Telegram interface."""
        super().__init__(session_prefix="telegram")

        if not is_telegram_enabled():
            raise ValueError("Telegram interface is disabled by deployment profile")

        self.application: Optional[Application] = None
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.webhook_url = TELEGRAM_WEBHOOK_URL

        # Session tracking per chat
        self.chat_sessions: dict = {}

        # Processing lock to prevent concurrent processing per chat
        self.processing_locks: dict = {}

        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set in environment")

        logger.info("TelegramInterface initialized")

    def _get_authorized_chat_ids(self) -> Set[str]:
        """
        Get set of authorized chat IDs.

        Returns:
            Set of authorized chat ID strings
        """
        authorized = set()

        # Primary chat ID
        if TELEGRAM_CHAT_ID:
            authorized.add(TELEGRAM_CHAT_ID)

        # Support for multiple chat IDs (comma-separated)
        additional = os.getenv("TELEGRAM_AUTHORIZED_CHAT_IDS", "")
        if additional:
            for chat_id in additional.split(","):
                chat_id = chat_id.strip()
                if chat_id:
                    authorized.add(chat_id)

        return authorized

    def _get_session_for_chat(self, chat_id: str) -> str:
        """
        Get or create session ID for a chat.

        Args:
            chat_id: Telegram chat ID

        Returns:
            Session ID string
        """
        # Every entry point calls this right before handing the message to the
        # agents, so it is also the one place that knows which chat the next
        # tool calls belong to. A job scheduled in this turn must answer here,
        # not in whatever chat TELEGRAM_CHAT_ID happens to point at.
        try:
            from tools.adk_tools.scheduler_adk_tools import set_delivery_target
            set_delivery_target(chat_id)
        except Exception:  # scheduler tools are optional in some profiles
            pass

        if chat_id not in self.chat_sessions:
            self.chat_sessions[chat_id] = self.generate_session_id(chat_id)
            logger.info(f"Created new session for chat {chat_id}: {self.chat_sessions[chat_id]}")

        return self.chat_sessions[chat_id]

    async def _get_processing_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create processing lock for a chat."""
        if chat_id not in self.processing_locks:
            self.processing_locks[chat_id] = asyncio.Lock()
        return self.processing_locks[chat_id]

    def format_response(self, response: str) -> str:
        """
        Format response for Telegram.

        Handles:
        - Escaping HTML special characters
        - Converting markdown to Telegram-compatible format

        Args:
            response: Raw response from agent

        Returns:
            Telegram-formatted response
        """
        # For now, return as-is (Telegram supports basic markdown)
        # If using HTML parse mode, escape special chars
        return response

    def _split_message(self, text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
        """
        Split long message into chunks.

        Args:
            text: Message text
            max_length: Maximum length per chunk

        Returns:
            List of message chunks
        """
        if len(text) <= max_length:
            return [text]

        chunks = []
        current_chunk = ""

        # Try to split on newlines first
        lines = text.split("\n")

        for line in lines:
            if len(current_chunk) + len(line) + 1 <= max_length:
                current_chunk += line + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""

                # If single line is too long, split it
                if len(line) > max_length:
                    while len(line) > max_length:
                        chunks.append(line[:max_length])
                        line = line[max_length:]
                    current_chunk = line + "\n"
                else:
                    current_chunk = line + "\n"

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def _is_supported_audio_mime(self, mime_type: str) -> bool:
        return mime_type in get_supported_audio_mime_types()

    # === Command Handlers ===

    @authorized_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        chat_id = str(update.effective_chat.id)
        user = update.effective_user

        # Create session for this chat
        session_id = self._get_session_for_chat(chat_id)

        welcome_message = f"""
*Google Workspace ADK Agent System*

Dobrodošao, {user.first_name}!

Ja sam AI asistent koji upravlja tvojim Google Workspace alatima.

*Dostupne komande:*
/start - Prikaži ovu poruku
/status - Status sustava
/agents - Lista dostupnih agenata
/classroom - Uđi u Philosophy Classroom
/leave - Izađi iz Philosophy Classroom
/reset - Resetiraj sesiju
/help - Pomoć

*Primjeri upita:*
- "Pošalji email Marku s temom sastanak"
- "Napravi dokument s izvještajem prodaje"
- "Koji su moji događaji za sutra?"
- "Pretraži web o AI trendovima"

Session ID: `{session_id}`
"""
        await update.message.reply_text(
            welcome_message,
            parse_mode=ParseMode.MARKDOWN
        )

    @authorized_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command."""
        status = self.get_status()

        status_text = f"""
*System Status*

Status: `{status.get('status', 'unknown')}`
Interface: `{status.get('interface', 'telegram')}`
Active Mode: `{status.get('active_mode', 'LEGACY')}`
Session: `{status.get('session_id', 'N/A')}`

Total Agents: {status.get('total_agents', 0)}
Worker Agents: {status.get('worker_agents', 0)}
ADK Migration: {status.get('adk_migration', 'N/A')}
"""
        await update.message.reply_text(
            status_text,
            parse_mode=ParseMode.MARKDOWN
        )

    @authorized_only
    async def cmd_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tokens command - per-agent token usage (cumulative). /tokens reset clears it."""
        from tools.observability.token_stats import get_token_stats

        stats = get_token_stats()
        if context.args and context.args[0].lower() == "reset":
            stats.reset()
            await update.message.reply_text("Token statistika resetirana.")
            return

        report = stats.report(title="TOKEN USAGE (cumulative)")
        safe = html.escape(report)
        await update.message.reply_text(f"<pre>{safe}</pre>", parse_mode=ParseMode.HTML)

    @authorized_only
    async def cmd_agents(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /agents command."""
        agents_info = self.get_agents_info()

        # Split if too long
        chunks = self._split_message(agents_info)

        for chunk in chunks:
            await update.message.reply_text(chunk)

    @authorized_only
    async def cmd_classroom(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /classroom command - enter Philosophy Classroom."""
        if self.system:
            self.system.active_mode = "CLASSROOM"
            await update.message.reply_text(
                "*Entering Philosophy Classroom...*\n\n"
                "Socrates awaits. Ask your philosophical questions.\n"
                "Type /leave to exit the classroom.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("System not initialized. Send a message first.")

    @authorized_only
    async def cmd_leave(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /leave command - exit Philosophy Classroom."""
        if self.system:
            self.system.active_mode = "LEGACY"
            await update.message.reply_text(
                "*Exiting Philosophy Classroom*\n\n"
                "Back to normal mode. How can I help you?",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("System not initialized.")

    @authorized_only
    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command - reset session."""
        chat_id = str(update.effective_chat.id)

        # Create new session
        if chat_id in self.chat_sessions:
            del self.chat_sessions[chat_id]

        new_session = self._get_session_for_chat(chat_id)

        # Reset mode
        if self.system:
            self.system.active_mode = "LEGACY"

        await update.message.reply_text(
            f"*Session Reset*\n\nNew session: `{new_session}`",
            parse_mode=ParseMode.MARKDOWN
        )

    @authorized_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        help_text = """
*Google Workspace ADK - Pomoć*

Ovaj bot ti omogućuje upravljanje Google Workspace alatima kroz prirodni jezik.

*Što mogu raditi:*
- Gmail: slanje, čitanje, pretraživanje emailova
- Calendar: kreiranje događaja, pregled rasporeda
- Drive: upravljanje datotekama, pretraživanje
- Docs: kreiranje i uređivanje dokumenata
- Sheets: rad s tablicama
- Contacts: pretraživanje kontakata
- Tasks: upravljanje zadacima
- Web Search: pretraživanje interneta
- YouTube: pretraživanje videa

*Philosophy Classroom:*
Poseban mod za filozofske diskusije sa Sokratom.
Koristi /classroom za ulazak.

*Savjeti:*
- Budi specifičan u zahtjevima
- Možeš kombinirati više akcija u jednom upitu
- Sustav pamti kontekst razgovora

*Primjeri:*
"Pošalji email na marko@firma.hr s naslovom Sastanak"
"Napravi događaj sutra u 15h naziva Tim meeting"
"Pronađi sve PDF-ove na Drive-u"
"""
        await update.message.reply_text(
            help_text,
            parse_mode=ParseMode.MARKDOWN
        )

    @authorized_only
    async def cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send the daily briefing (/pregled)."""
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
        try:
            from config.user_context import get_default_user_context
            from services.daily_briefing import get_daily_briefing

            text = await get_daily_briefing(
                get_default_user_context(channel="telegram")
            )
        except Exception as e:
            logger.error(f"Daily briefing failed: {e}")
            text = "Ne mogu trenutno sastaviti dnevni pregled. Pokušaj ponovno kasnije."

        for chunk in self._split_message(text):
            await update.message.reply_text(chunk)

    # === Message Handler ===

    @authorized_only
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle regular text messages."""
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        message_text = update.message.text

        if message_text and len(message_text) > MAX_INPUT_TEXT_LENGTH:
            await update.message.reply_text(
                f"Poruka je preduga ({len(message_text)} znakova, "
                f"maksimum {MAX_INPUT_TEXT_LENGTH}). Skrati je i pošalji ponovno."
            )
            return

        # Get processing lock for this chat. A message arriving while the
        # previous one is still running gets told so, rather than sitting on
        # the lock until Telegram's user gives up and assumes it is broken.
        # (handle_photo/document/voice below still queue: they are rarely the
        # message someone sends to check whether the assistant is alive.)
        lock = await self._get_processing_lock(chat_id)
        if not await self.acquire_or_busy(lock, chat_id):
            await update.message.reply_text(self.busy_notice(chat_id))
            return

        try:
            self._note_turn_start(chat_id, message_text)
            try:
                # Show typing indicator
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING
                )

                # Get session
                session_id = self._get_session_for_chat(chat_id)

                logger.info(f"Processing message from {chat_id}: {message_text[:50]}...")

                # Process through agent system
                response = await self.process_message(
                    user_id=user_id,
                    message=message_text,
                    session_id=session_id
                )

                # Format and send response
                formatted = self.format_response(response)
                chunks = self._split_message(formatted)

                for i, chunk in enumerate(chunks):
                    # Show typing for subsequent chunks
                    if i > 0:
                        await context.bot.send_chat_action(
                            chat_id=chat_id,
                            action=ChatAction.TYPING
                        )
                        await asyncio.sleep(0.5)

                    await update.message.reply_text(chunk)

                logger.info(f"Response sent to {chat_id}")

            except Exception as e:
                logger.error(f"Error handling message: {e}")
                await update.message.reply_text(
                    f"Greška pri obradi zahtjeva: {str(e)}"
                )
        finally:
            self._note_turn_end(chat_id)
            lock.release()

    # === Photo & Document Handlers ===

    async def _process_image_ocr(self, image_bytes: bytes, mime_type: str, chat_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Process an image through OCR and route the results to the orchestrator.

        Args:
            image_bytes: Raw image bytes
            mime_type: MIME type of the image
            chat_id: Telegram chat ID
            update: Telegram update object
            context: Telegram context
        """
        user_id = str(update.effective_user.id)
        session_id = self._get_session_for_chat(chat_id)

        # Step 1: OCR extraction
        await update.message.reply_text("Primio sam sliku. Pokrecem OCR ekstrakciju...")

        try:
            from tools.handlers.vision_handler import VisionHandler
            handler = VisionHandler()

            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            receipt_data = await handler.extract_receipt_data(
                image_data=image_b64,
                mime_type=mime_type
            )

            confidence = receipt_data.get('confidence_score', 0)
            merchant = receipt_data.get('merchant_name', 'Nepoznat')
            total = receipt_data.get('total_amount', 0)
            currency = receipt_data.get('currency', 'EUR')
            date = receipt_data.get('transaction_date', 'N/A')
            category = receipt_data.get('expense_category', 'Ostalo')
            invoice_num = receipt_data.get('receipt_number', 'N/A')

            # Step 2: Show extracted data to user
            confidence_emoji = "HIGH" if confidence >= 0.8 else "LOW" if confidence >= 0.5 else "VERY LOW"
            summary = (
                f"*OCR rezultat* (confidence: {confidence:.0%} - {confidence_emoji})\n\n"
                f"Dobavljac: {merchant}\n"
                f"Datum: {date}\n"
                f"Broj racuna: {invoice_num}\n"
                f"Iznos: {total} {currency}\n"
                f"Kategorija: {category}\n"
            )

            items = receipt_data.get('items', [])
            if items:
                summary += f"\nStavke ({len(items)}):\n"
                for item in items[:5]:
                    desc = item.get('description', item.get('name', '?'))
                    amt = item.get('amount', item.get('price', ''))
                    summary += f"  - {desc}: {amt}\n"
                if len(items) > 5:
                    summary += f"  ... i jos {len(items) - 5} stavki\n"

            await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

            # Step 3: Route through agent system for saving
            if confidence >= 0.5:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

                # Build instruction for the orchestrator
                save_instruction = (
                    f"Spremi ovaj racun u bazu (iz Telegram OCR-a). "
                    f"OCR podaci (JSON): {json.dumps(receipt_data, ensure_ascii=False)}. "
                    f"Confidence: {confidence:.2f}. "
                )
                if confidence >= 0.8:
                    save_instruction += "Confidence je visok. Spremi podatke o racunu."
                else:
                    save_instruction += "Confidence je nizak, spremi ali oznaci za manual review."

                response = await self.process_message(
                    user_id=user_id,
                    message=save_instruction,
                    session_id=session_id
                )

                formatted = self.format_response(response)
                chunks = self._split_message(formatted)
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(
                    "Confidence je prenizak za automatsko spremanje. "
                    "Posalji bolju sliku ili unesi podatke rucno."
                )

        except Exception as e:
            logger.error(f"OCR processing failed: {e}")
            await update.message.reply_text(
                f"Greska pri OCR obradi: {str(e)}\n"
                "Pokusaj poslati jasniju sliku."
            )

    @authorized_only
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages - OCR receipt/invoice processing."""
        chat_id = str(update.effective_chat.id)
        lock = await self._get_processing_lock(chat_id)

        async with lock:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

                # Get highest resolution photo
                photo = update.message.photo[-1]

                # Reject oversized photos BEFORE downloading them
                if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
                    await update.message.reply_text(
                        f"Fotografija je prevelika ({photo.file_size} B, "
                        f"maksimum {MAX_PHOTO_BYTES} B)."
                    )
                    return

                file = await context.bot.get_file(photo.file_id)

                # Download photo
                photo_bytes = await file.download_as_bytearray()

                logger.info(f"Received photo from {chat_id}: {photo.file_id} ({len(photo_bytes)} bytes)")

                await self._process_image_ocr(
                    image_bytes=bytes(photo_bytes),
                    mime_type="image/jpeg",
                    chat_id=chat_id,
                    update=update,
                    context=context
                )

            except Exception as e:
                logger.error(f"Error handling photo: {e}")
                await update.message.reply_text(f"Greska pri obradi fotografije: {str(e)}")

    @authorized_only
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle document messages - OCR for PDFs and image files."""
        chat_id = str(update.effective_chat.id)
        lock = await self._get_processing_lock(chat_id)

        async with lock:
            try:
                document = update.message.document
                file_name = document.file_name or "unknown"
                mime_type = document.mime_type or ""

                # Supported file types
                supported_image = {"image/jpeg", "image/png", "image/bmp", "image/x-ms-bmp"}
                supported_pdf = {"application/pdf"}
                supported = supported_image | supported_pdf

                if mime_type not in supported:
                    await update.message.reply_text(
                        f"Nepodrzani format: {mime_type}\n"
                        "Podrzani formati: JPEG, PNG, BMP, PDF"
                    )
                    return

                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

                # Download document
                file = await context.bot.get_file(document.file_id)
                doc_bytes = await file.download_as_bytearray()

                logger.info(f"Received document from {chat_id}: {file_name} ({mime_type}, {len(doc_bytes)} bytes)")

                await self._process_image_ocr(
                    image_bytes=bytes(doc_bytes),
                    mime_type=mime_type,
                    chat_id=chat_id,
                    update=update,
                    context=context
                )

            except Exception as e:
                logger.error(f"Error handling document: {e}")
                await update.message.reply_text(f"Greska pri obradi dokumenta: {str(e)}")

    @authorized_only
    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Telegram voice notes and audio files through shared STT ingress."""
        chat_id = str(update.effective_chat.id)
        user_id = f"telegram-user-{chat_id}"
        session_id = self._get_session_for_chat(chat_id)
        lock = await self._get_processing_lock(chat_id)

        async with lock:
            try:
                voice = update.message.voice
                audio = update.message.audio
                audio_obj = voice or audio
                if audio_obj is None:
                    await update.message.reply_text("Audio poruka nije prepoznata.")
                    return

                duration = getattr(audio_obj, "duration", 0) or 0
                mime_type = getattr(audio_obj, "mime_type", None) or "audio/ogg"
                file_id = getattr(audio_obj, "file_id", "")
                file_name = getattr(audio_obj, "file_name", "telegram-audio")
                source = "telegram_voice" if voice else "telegram_audio"

                if duration > MAX_AUDIO_DURATION_SECONDS:
                    await update.message.reply_text(
                        f"Audio je predug ({duration}s). Maksimum je {MAX_AUDIO_DURATION_SECONDS}s."
                    )
                    return

                if not self._is_supported_audio_mime(mime_type):
                    await update.message.reply_text(
                        f"Nepodrzani audio format: {mime_type}. "
                        "Podrzani su OGG/Opus, MP3, WAV, M4A i WebM."
                    )
                    return

                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

                tg_file = await context.bot.get_file(file_id)
                audio_bytes = bytes(await tg_file.download_as_bytearray())

                transcript_result = await get_audio_ingress_service().transcribe_audio(
                    audio_bytes=audio_bytes,
                    mime_type=mime_type,
                    source=source,
                    metadata={
                        "chat_id": chat_id,
                        "duration_seconds": duration,
                        "telegram_file_id": file_id,
                        "file_name": file_name,
                    },
                )
                transcript = transcript_result.transcript.strip()
                if not transcript:
                    raise AudioIngressError("Prazan transcript")

                logger.info(
                    "telegram_voice audit source=%s chat_id=%s duration=%ss transcript_chars=%s",
                    source,
                    chat_id,
                    duration,
                    len(transcript),
                )

                await update.message.reply_text(
                    f"_Transkript:_ {transcript}",
                    parse_mode=ParseMode.MARKDOWN,
                )

                response = await self.process_message(
                    user_id=user_id,
                    message=transcript,
                    session_id=session_id,
                )

                formatted = self.format_response(response)
                for chunk in self._split_message(formatted):
                    await update.message.reply_text(chunk)

            except AudioIngressError as e:
                logger.warning(f"Telegram audio transcription failed: {e}")
                await update.message.reply_text(
                    "Nisam uspjela prepisati audio poruku. Posalji kracu i jasniju snimku ili tekst."
                )
            except Exception as e:
                logger.error(f"Error handling Telegram audio: {e}")
                await update.message.reply_text(
                    "Dogodila se greska pri obradi audio poruke. Pokusaj ponovno."
                )

    # === Application Setup ===

    def _setup_handlers(self):
        """Setup command and message handlers."""
        if self.application is None:
            return

        # Commands
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("tokens", self.cmd_tokens))
        self.application.add_handler(CommandHandler("agents", self.cmd_agents))
        self.application.add_handler(CommandHandler("classroom", self.cmd_classroom))
        self.application.add_handler(CommandHandler("leave", self.cmd_leave))
        self.application.add_handler(CommandHandler("reset", self.cmd_reset))
        self.application.add_handler(CommandHandler("pregled", self.cmd_briefing))
        self.application.add_handler(CommandHandler("help", self.cmd_help))

        # Regular messages
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        # Photo messages (receipt/invoice OCR)
        self.application.add_handler(
            MessageHandler(filters.PHOTO, self.handle_photo)
        )

        # Voice/audio messages (STT -> agent)
        self.application.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice)
        )

        # Document messages (PDF/image files)
        self.application.add_handler(
            MessageHandler(filters.Document.ALL, self.handle_document)
        )

        logger.info("Handlers registered (text + photo + document + voice)")

    async def _setup_bot_commands(self):
        """Setup bot command menu in Telegram."""
        commands = [
            BotCommand("start", "Pokreni bot"),
            BotCommand("status", "Status sustava"),
            BotCommand("tokens", "Potrošnja tokena po agentima"),
            BotCommand("agents", "Lista agenata"),
            BotCommand("classroom", "Philosophy Classroom"),
            BotCommand("leave", "Izađi iz Classroom-a"),
            BotCommand("reset", "Resetiraj sesiju"),
            BotCommand("pregled", "Dnevni pregled (email, kalendar, zadaci, vrijeme)"),
            BotCommand("help", "Pomoć"),
        ]

        await self.application.bot.set_my_commands(commands)
        logger.info("Bot commands menu set")

    # === Start/Stop Methods ===

    async def start(self) -> None:
        """
        Start the Telegram interface.

        Uses webhook if TELEGRAM_WEBHOOK_URL is set, otherwise polling.
        """
        logger.info("Starting Telegram interface...")

        # Initialize agent system
        self.initialize_system()

        # Build application
        self.application = ApplicationBuilder().token(self.bot_token).build()

        # Setup handlers
        self._setup_handlers()

        # Initialize and setup bot commands
        await self.application.initialize()
        await self._setup_bot_commands()

        if self.webhook_url:
            await self._start_webhook()
        else:
            await self._start_polling()

    async def _start_polling(self):
        """Start bot in polling mode (for local development)."""
        logger.info("Starting Telegram bot in POLLING mode...")
        logger.info(f"Authorized chat IDs: {self._get_authorized_chat_ids()}")

        # Start polling
        await self.application.start()
        await self.application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

        logger.info("Telegram bot is running (polling mode)")
        logger.info("Press Ctrl+C to stop")

        notify_ready("polling")

        # Keep running until interrupted -- while proving we are still alive.
        try:
            await self._supervise_polling()
        except asyncio.CancelledError:
            pass

    async def _supervise_polling(self) -> None:
        """Idle loop that verifies the bot is still actually polling.

        Without this the process can sit in ``await asyncio.sleep(1)`` forever
        while the updater underneath is dead -- systemd sees a live PID and
        reports ``active (running)``, so nothing ever restarts it. Raising
        :class:`PollingUnhealthy` hands control to the entry point, which exits
        hard so ``Restart=always`` takes over.
        """
        enabled = os.getenv("TELEGRAM_HEALTHCHECK_ENABLED", "true").lower() != "false"
        local_every = float(os.getenv("TELEGRAM_HEALTHCHECK_INTERVAL_SECONDS", "30"))
        ping_every = float(os.getenv("TELEGRAM_HEALTHCHECK_PING_SECONDS", "300"))
        max_failures = int(os.getenv("TELEGRAM_HEALTHCHECK_MAX_FAILURES", "3"))
        heartbeat_every = watchdog_interval_seconds()

        tick = min(
            [v for v in (local_every, ping_every, heartbeat_every) if v and v > 0]
            or [30.0]
        )
        since_local = since_ping = since_heartbeat = 0.0
        ping_failures = 0

        while True:
            await asyncio.sleep(tick)
            if not enabled:
                continue

            since_local += tick
            since_ping += tick
            since_heartbeat += tick

            healthy = True

            if since_local >= local_every:
                since_local = 0.0
                reason = self._polling_stopped_reason()
                if reason:
                    raise PollingUnhealthy(reason)

            if ping_every > 0 and since_ping >= ping_every:
                since_ping = 0.0
                try:
                    await asyncio.wait_for(self.application.bot.get_me(), timeout=30)
                    if ping_failures:
                        logger.info("Telegram reachable again after %s failed ping(s)", ping_failures)
                    ping_failures = 0
                except Exception as exc:
                    ping_failures += 1
                    healthy = False
                    logger.warning(
                        "Telegram health ping failed (%s/%s): %s",
                        ping_failures, max_failures, exc,
                    )
                    if ping_failures >= max_failures:
                        raise PollingUnhealthy(
                            f"getMe failed {ping_failures} times in a row: {exc}"
                        )

            # Only tell systemd we are fine when we believe it.
            if heartbeat_every and healthy and since_heartbeat >= heartbeat_every:
                since_heartbeat = 0.0
                notify_watchdog("polling")

    def _polling_stopped_reason(self) -> Optional[str]:
        """Describe why polling is no longer running, or None while healthy."""
        app = self.application
        if app is None:
            return "application is gone"
        if not getattr(app, "running", False):
            return "application stopped running"
        updater = getattr(app, "updater", None)
        if updater is None:
            return "updater is gone"
        if not getattr(updater, "running", False):
            return "updater stopped polling"
        return None

    async def _start_webhook(self):
        """Start bot in webhook mode (for Cloud Run)."""
        logger.info(f"Starting Telegram bot in WEBHOOK mode: {self.webhook_url}")

        # Secret token: Telegram echoes it back in the
        # X-Telegram-Bot-Api-Secret-Token header, letting us reject forged
        # POSTs that spoof an authorized chat_id.
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        if not secret:
            logger.warning(
                "TELEGRAM_WEBHOOK_SECRET is not set — webhook updates cannot "
                "be authenticated and forged POSTs would be processed."
            )

        # Set webhook
        await self.application.bot.set_webhook(
            url=self.webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            secret_token=secret or None,
        )

        logger.info("Webhook set successfully")

        # Note: In webhook mode, the actual HTTP server needs to be set up
        # externally (e.g., FastAPI, Flask) and call application.process_update()

    async def stop(self) -> None:
        """Stop the Telegram interface.

        Runs from a ``finally:`` block, so it must never raise: an exception here
        replaces whatever error actually brought the bot down. On 2026-08-18 a
        startup ``TimedOut`` was masked by ``RuntimeError: This Updater is not
        running!`` raised while tearing down an updater that never started.
        Every step is therefore both state-checked and exception-guarded.
        """
        logger.info("Stopping Telegram interface...")

        app = self.application
        if app is None:
            logger.info("Telegram interface stopped (nothing to tear down)")
            return

        updater = getattr(app, "updater", None)
        if updater is not None and getattr(updater, "running", False):
            await self._safe_teardown("updater.stop", updater.stop())

        if getattr(app, "running", False):
            await self._safe_teardown("application.stop", app.stop())

        # shutdown() is idempotent and must run even if the steps above failed,
        # otherwise the HTTP pool and the bot session leak.
        await self._safe_teardown("application.shutdown", app.shutdown())

        logger.info("Telegram interface stopped")

    @staticmethod
    async def _safe_teardown(step: str, coro, timeout: float = 15.0) -> None:
        """Await one shutdown step, bounded in time and never raising.

        The timeout matters as much as the try/except: a wedged updater that
        blocks here would keep the process alive exactly like the incident we
        are guarding against.
        """
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Shutdown step '%s' timed out after %ss (ignored)", step, timeout)
        except Exception as exc:
            logger.warning("Shutdown step '%s' failed (ignored): %s", step, exc)

    # === Webhook Handler (for Cloud Run) ===

    async def process_webhook_update(
        self,
        update_data: dict,
        secret_header: Optional[str] = None,
    ) -> None:
        """
        Process incoming webhook update.

        This method should be called by the HTTP server when receiving
        updates from Telegram. The server MUST pass the value of the
        X-Telegram-Bot-Api-Secret-Token request header as ``secret_header`` —
        with TELEGRAM_WEBHOOK_SECRET configured, updates that do not carry the
        matching token are rejected (forged POSTs could otherwise spoof an
        authorized chat_id).

        Args:
            update_data: Raw update data from Telegram webhook
            secret_header: Value of X-Telegram-Bot-Api-Secret-Token, if any

        Raises:
            PermissionError: If the secret token does not match
        """
        if self.application is None:
            raise RuntimeError("Application not initialized")

        expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        if expected:
            import hmac
            if not secret_header or not hmac.compare_digest(expected, secret_header):
                logger.warning("Rejected webhook update with missing/invalid secret token")
                raise PermissionError("Invalid Telegram webhook secret token")

        update = Update.de_json(update_data, self.application.bot)
        await self.application.process_update(update)
