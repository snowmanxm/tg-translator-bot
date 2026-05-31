from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageEntityCode, MessageEntityPre
from telethon.utils import get_display_name

from translator_bot.ai import OpenAIService
from translator_bot.bot_api import send_bot_media, send_bot_message, set_bot_commands
from translator_bot.config import ChatSettings, Settings, load_settings
from translator_bot.config_watcher import ConfigWatcher
from translator_bot.formatting import (
    format_summary,
    format_translation,
    ProtectedRange,
    mask_protected_ranges,
    protected_segments_for_ai,
    restore_protected_segments,
    split_telegram_message,
)
from translator_bot.knowledge import KnowledgeCache, KnowledgeStatus
from translator_bot.language import contains_chinese
from translator_bot.scheduler import SummaryScheduler
from translator_bot.storage import MongoStorage


logger = logging.getLogger(__name__)


class TelegramTranslatorApp:
    def __init__(self, settings: Settings, *, config_path: str | Path = "config.yaml") -> None:
        self.settings = settings
        self.config_path = Path(config_path)
        self.client = TelegramClient(
            settings.telegram.session_name,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )
        self.bot_client = TelegramClient(
            settings.telegram.bot_session_name,
            settings.telegram.api_id,
            settings.telegram.api_hash,
        )
        self.ai = OpenAIService(settings)
        self.storage = MongoStorage(settings)
        self.scheduler = SummaryScheduler(settings)
        self.knowledge_cache = KnowledgeCache(settings.reply_suggestions.knowledge)
        self.reply_suggestions_enabled_override: bool | None = None
        self.reply_suggestions_chat_overrides: dict[int, bool | None] = {}
        self.reply_count_override: int | None = None
        self.config_watcher: ConfigWatcher | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.storage.setup()
        self._register_handlers()
        self.scheduler.configure(
            hourly_job=self.send_hourly_summaries,
            daily_job=self.send_daily_summaries,
            prune_job=self.storage.prune_old_messages,
        )
        self._configure_knowledge_reload_job()
        self.scheduler.start()
        await self.reload_knowledge()
        if self.settings.runtime.config_reload_enabled:
            self.config_watcher = ConfigWatcher(self.config_path, self._schedule_config_reload)
            self.config_watcher.start()

        await self.client.start()
        await self.bot_client.start(bot_token=self.settings.telegram.bot_token)
        await self._setup_bot_commands()
        logger.info("Telegram watcher and sender bot started")
        try:
            await self._send_to_destination(
                self.settings.telegram.send_translations_to_chat_id,
                "Translator bot started.",
            )
            logger.info("Startup notification sent")
        except Exception:
            logger.exception("Failed to send startup notification; continuing to watch messages")
        try:
            await self.client.run_until_disconnected()
        finally:
            self.scheduler.shutdown()
            if self.config_watcher:
                self.config_watcher.stop()
            await self.bot_client.disconnect()
            await self.storage.close()

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage(incoming=True))
        async def incoming_handler(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_incoming(event.message)
            except Exception:
                logger.exception("Failed to handle incoming message")

        @self.bot_client.on(events.NewMessage(incoming=True))
        async def bot_command_handler(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_command(event.message)
            except Exception:
                logger.exception("Failed to handle bot command")

    async def _handle_incoming(self, message: Message) -> None:
        text = message.raw_text or ""
        attachment = _attachment_metadata(message)
        if not text and not attachment:
            return
        masked_text, formatted_text, protected_segments = mask_protected_ranges(
            text,
            _protected_ranges_from_entities(text, message.entities or []),
        )

        chat_id = int(message.chat_id or 0)
        sender_id = int(message.sender_id or 0)
        chat_settings = self.settings.chat_for(chat_id)
        if not chat_settings or not chat_settings.enabled:
            logger.info("Skipping message from unconfigured or disabled chat_id=%s", chat_id)
            return
        if chat_id in self.settings.ignore.chats or sender_id in self.settings.ignore.users:
            logger.info("Skipping ignored chat_id=%s sender_id=%s", chat_id, sender_id)
            return

        chat = await message.get_chat()
        sender = await message.get_sender()
        chat_title = get_display_name(chat) or str(chat_id)
        chat_memory = await self.storage.get_chat_memory(chat_id)
        chat_title_translation = _chat_title_translation_from_memory(chat_memory, current_title=chat_title)
        sender_name = get_display_name(sender) or str(sender_id)
        sender_username = getattr(sender, "username", None)
        is_chinese = contains_chinese(text)
        message_date = message.date.astimezone(UTC) if message.date else datetime.now(UTC)

        document = {
            "chat_id": chat_id,
            "message_id": message.id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "text": text,
            "attachment": attachment,
            "date": message_date,
            "contains_chinese": is_chinese,
            "translation": None,
            "important": False,
            "alerts": {},
        }

        if is_chinese:
            logger.info("Analyzing Chinese message chat_id=%s message_id=%s", chat_id, message.id)
            analysis = await self.ai.analyze_message(
                masked_text,
                chat_title=chat_title,
                sender_name=sender_name,
                protected_placeholders=protected_segments_for_ai(protected_segments),
            )
            translation = restore_protected_segments(analysis.message_english, protected_segments, append_missing=True)
            alerts = analysis.alerts
            important = analysis.important
            document.update(
                {
                    "translation": translation,
                    "important": important,
                    "alerts": alerts,
                }
            )
            important_reason = _important_reason(analysis.reason, alerts) if important else None
            if important_reason:
                document["important_reason"] = important_reason
            suggested_replies = await self._maybe_suggest_replies(
                chat_settings=chat_settings,
                chat_memory=chat_memory,
                chat_title=chat_title,
                current_message=document,
                alerts=alerts,
                important=important,
            )
            if suggested_replies:
                document["suggested_replies"] = suggested_replies
            if self._should_send_translation(chat_settings, important):
                await self._send_translation(
                    source_message=message,
                    chat_title=chat_title,
                    chat_title_translation=chat_title_translation,
                    sender_name=sender_name,
                    sender_username=sender_username,
                    original=formatted_text,
                    translation=translation,
                    important=important,
                    alerts=alerts,
                    suggested_replies=suggested_replies,
                )
                logger.info("Sent translation chat_id=%s message_id=%s", chat_id, message.id)
        else:
            logger.info("Stored non-Chinese message chat_id=%s message_id=%s", chat_id, message.id)
            if attachment and self._should_send_attachment_only(chat_settings):
                await self._send_translation(
                    source_message=message,
                    chat_title=chat_title,
                    chat_title_translation=chat_title_translation,
                    sender_name=sender_name,
                    sender_username=sender_username,
                    original=formatted_text,
                    translation="",
                    important=False,
                    alerts=None,
                )

        await self.storage.save_message(document)
        await self.storage.touch_chat_memory_metadata(
            chat_id=chat_id,
            chat_title=chat_title,
            last_message_id=message.id,
            last_message_at=message_date,
        )

    def _should_send_translation(self, chat_settings: ChatSettings, important: bool) -> bool:
        if not self.settings.features.instant_translation:
            return False
        if not chat_settings.instant_translation or chat_settings.muted:
            return False
        if chat_settings.important_only and not important:
            return False
        return True

    def _should_send_attachment_only(self, chat_settings: ChatSettings) -> bool:
        if not self.settings.attachments.enabled or not self.settings.attachments.forward_displayable:
            return False
        if not self.settings.features.instant_translation:
            return False
        if not chat_settings.instant_translation or chat_settings.muted or chat_settings.important_only:
            return False
        return True

    def _reply_suggestions_enabled(self, chat_settings: ChatSettings) -> bool:
        global_enabled = (
            self.reply_suggestions_enabled_override
            if self.reply_suggestions_enabled_override is not None
            else self.settings.reply_suggestions.enabled
        )
        chat_override = self.reply_suggestions_chat_overrides.get(chat_settings.id, chat_settings.reply_suggestions)
        if chat_override is None:
            return bool(global_enabled)
        return bool(chat_override)

    def _reply_count(self) -> int:
        return self.reply_count_override or self.settings.reply_suggestions.count

    async def _maybe_suggest_replies(
        self,
        *,
        chat_settings: ChatSettings,
        chat_memory: dict[str, Any] | None,
        chat_title: str,
        current_message: dict[str, Any],
        alerts: dict[str, bool],
        important: bool,
    ) -> list[dict[str, str]]:
        if not self._reply_suggestions_enabled(chat_settings):
            return []
        if not (alerts.get("question_or_request") or alerts.get("name_mention") or alerts.get("urgent")):
            return []

        chat_id = int(current_message["chat_id"])
        effective_memory = chat_memory or {"chat_id": chat_id, "chat_title": chat_title}
        recent = await self.storage.recent_messages(
            chat_id=chat_id,
            limit=self.settings.reply_suggestions.recent_messages,
        )
        memory_map = {chat_id: effective_memory}
        current_payload = _message_for_ai(current_message, memory_map)
        recent_payload = [_message_for_ai(row, memory_map) for row in recent]
        try:
            replies = await self.ai.suggest_replies(
                current_message=current_payload,
                recent_messages=recent_payload,
                chat_memory=_reply_chat_memory_payload(effective_memory),
                profile=self.settings.reply_suggestions.profile.model_dump(),
                knowledge=self.knowledge_cache.content,
                max_count=self._reply_count(),
            )
        except Exception:
            logger.exception("Failed to generate reply suggestions")
            return []
        return [{"zh": reply.zh, "en": reply.en} for reply in replies]

    async def _send_translation(
        self,
        source_message: Message | None,
        chat_title: str,
        chat_title_translation: str | None,
        sender_name: str,
        sender_username: str | None,
        original: str,
        translation: str,
        important: bool = False,
        alerts: dict[str, bool] | None = None,
        suggested_replies: list[dict[str, str]] | None = None,
    ) -> None:
        include_original = self.settings.features.original_plus_translation
        message = format_translation(
            chat_title=chat_title,
            chat_title_translation=chat_title_translation,
            sender_name=sender_name,
            sender_username=sender_username,
            original=original,
            translation=translation,
            include_original=include_original,
            important=important,
            alerts=alerts,
            suggested_replies=suggested_replies,
        )
        await self._send_to_destination(
            self.settings.telegram.send_translations_to_chat_id,
            message,
            source_message=source_message,
        )

    async def _handle_command(self, message: Message) -> None:
        text = (message.raw_text or "").strip()
        prefix = self.settings.runtime.command_prefix
        if not text.startswith(prefix):
            return
        if self.settings.telegram.control_chat_id is not None and message.chat_id != int(self.settings.telegram.control_chat_id):
            return

        parts = text.split()
        command = parts[0].removeprefix(prefix).replace("-", "_").lower()
        args = parts[1:]
        handlers = {
            "start": self._cmd_help,
            "help": self._cmd_help,
            "list_chats": self._cmd_list_chats,
            "chats": self._cmd_list_chats,
            "reload_config": self._cmd_reload_config,
            "reload": self._cmd_reload_config,
            "config_status": self._cmd_config_status,
            "status": self._cmd_config_status,
            "test_send": self._cmd_test_send,
            "summary": self._cmd_summary,
            "translate_last": self._cmd_translate_last,
            "tlast": self._cmd_translate_last,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "enable": self._cmd_enable,
            "disable": self._cmd_disable,
            "important_only": self._cmd_important_only,
            "reply_suggestions": self._cmd_reply_suggestions,
            "reply_count": self._cmd_reply_count,
            "reload_knowledge": self._cmd_reload_knowledge,
            "knowledge_status": self._cmd_knowledge_status,
            "ignored_users": self._cmd_ignored_users,
            "ignore_user": self._cmd_ignore_user,
            "unignore_user": self._cmd_unignore_user,
            "ignored_chats": self._cmd_ignored_chats,
            "ignore_chat": self._cmd_ignore_chat,
            "unignore_chat": self._cmd_unignore_chat,
        }
        handler = handlers.get(command)
        if handler is None:
            await message.respond("Unknown command. Use /help.")
            return
        try:
            response = await handler(args, message)
        except Exception as exc:
            response = f"Command failed: {exc}"
        if response:
            await message.respond(response)

    async def send_hourly_summaries(self) -> None:
        await self._send_summaries(period="hourly", fallback_since=datetime.now(UTC) - timedelta(hours=1))

    async def send_daily_summaries(self) -> None:
        await self._send_summaries(period="daily", fallback_since=datetime.now(UTC) - timedelta(days=1))

    async def _send_summaries(self, *, period: str, fallback_since: datetime) -> None:
        key = f"last_{period}_summary"
        since = await self.storage.get_last_summary_time(key) or fallback_since
        now = datetime.now(UTC)
        watched_ids = [chat.id for chat in self.settings.chats if chat.enabled and chat.summaries]

        if self.settings.summary.combined_summary:
            messages = await self.storage.recent_messages(since=since, limit=500)
            messages = [msg for msg in messages if msg["chat_id"] in watched_ids]
            chat_memories = await self.storage.get_chat_memories(watched_ids)
            await self._summarize_and_send(
                messages,
                f"{period.title()} Combined Summary",
                chat_memories=chat_memories,
            )

        if self.settings.summary.per_chat_summary:
            for chat_id in watched_ids:
                messages = await self.storage.recent_messages(chat_id=chat_id, since=since, limit=250)
                chat_memory = await self.storage.get_chat_memory(chat_id)
                chat_settings = self.settings.chat_for(chat_id)
                chat_name = _chat_title_from_memory(chat_memory) or (chat_settings.name if chat_settings else str(chat_id))
                chat_name_translation = _chat_title_translation_from_memory(chat_memory, current_title=chat_name)
                await self._summarize_and_send(
                    messages,
                    f"{period.title()} Summary",
                    chat_name=chat_name,
                    chat_name_translation=chat_name_translation,
                    chat_memories={chat_id: chat_memory} if chat_memory else {},
                    update_memory=True,
                )

        await self.storage.set_last_summary_time(key, now)

    async def _summarize_and_send(
        self,
        messages: list[dict[str, Any]],
        title: str,
        chat_name: str | None = None,
        chat_name_translation: str | None = None,
        chat_memories: dict[int, dict[str, Any]] | None = None,
        update_memory: bool = False,
    ) -> None:
        if not messages:
            return
        payload = [_message_for_ai(message, chat_memories or {}) for message in messages]
        summary = await self.ai.summarize(payload, title=title)
        if update_memory:
            updated_memory = await self._update_chat_memory_from_summary(messages, summary)
            chat_name = chat_name or _chat_title_from_memory(updated_memory)
            chat_name_translation = chat_name_translation or _chat_title_translation_from_memory(
                updated_memory,
                current_title=chat_name,
            )
        summary_timezone = ZoneInfo(self.settings.summary.timezone)
        formatted = format_summary(
            title,
            summary,
            datetime.now(summary_timezone),
            chat_name=chat_name,
            chat_name_translation=chat_name_translation,
            timezone_name=self.settings.summary.timezone,
        )
        await self._send_to_destination(self.settings.telegram.send_summaries_to_chat_id, formatted)

    async def _update_chat_memory_from_summary(self, messages: list[dict[str, Any]], summary: str) -> dict[str, Any] | None:
        if not messages:
            return None
        chat_id = int(messages[0]["chat_id"])
        previous = await self.storage.get_chat_memory(chat_id)
        chat_title = _chat_title_from_memory(previous) or str(chat_id)
        payload = [_message_for_ai(message, {chat_id: previous} if previous else {}) for message in messages]
        update = await self.ai.update_chat_memory(
            chat_title=chat_title,
            previous_memory=previous.get("memory") if previous else None,
            recent_messages=payload,
            summary=summary,
        )
        now = datetime.now(UTC)
        last_message = messages[-1]
        document = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "chat_title_translation": update["chat_title_translation"],
            "chat_title_translation_stale": False,
            "memory": update["memory"],
            "stats": {
                "message_count_seen": previous.get("stats", {}).get("message_count_seen", len(messages)) if previous else len(messages),
                "last_message_id": last_message.get("message_id"),
                "last_message_at": last_message.get("date"),
                "last_memory_update_at": now,
            },
            "updated_at": now,
        }
        await self.storage.upsert_chat_memory(document)
        return document

    async def _setup_bot_commands(self) -> None:
        await set_bot_commands(
            self.settings.telegram.bot_token,
            [
                {"command": "help", "description": "Show available commands"},
                {"command": "list_chats", "description": "List watcher account chats"},
                {"command": "config_status", "description": "Show active config summary"},
                {"command": "reload_config", "description": "Reload config.yaml"},
                {"command": "test_send", "description": "Send test messages"},
                {"command": "summary", "description": "Generate a manual summary"},
                {"command": "translate_last", "description": "Translate recent stored messages"},
                {"command": "reply_suggestions", "description": "Show or change reply suggestion switch"},
                {"command": "reply_count", "description": "Show or change reply suggestion count"},
                {"command": "reload_knowledge", "description": "Reload reply knowledge markdown files"},
                {"command": "knowledge_status", "description": "Show reply knowledge cache status"},
                {"command": "ignored_users", "description": "List ignored users"},
                {"command": "ignored_chats", "description": "List ignored chats"},
            ],
        )

    async def _send_to_destination(
        self,
        destination: str | int,
        text: str,
        *,
        source_message: Message | None = None,
    ) -> None:
        if isinstance(destination, str) and destination.strip().lstrip("-").isdigit():
            destination = int(destination)
        media_path: Path | None = None
        media_type: str | None = None
        if source_message and self.settings.attachments.enabled and self.settings.attachments.forward_displayable:
            media_type = _displayable_media_type(source_message)
            if media_type and _media_within_size_limit(source_message, self.settings.attachments.download_max_mb):
                media_path = await self._download_attachment(source_message)

        if media_path and media_type:
            try:
                if len(text) <= 1024 and _media_supports_caption(media_type):
                    await send_bot_media(
                        self.settings.telegram.bot_token,
                        destination,
                        media_path,
                        media_type=media_type,
                        caption=text,
                        parse_mode="HTML",
                    )
                    return
                await send_bot_media(
                    self.settings.telegram.bot_token,
                    destination,
                    media_path,
                    media_type=media_type,
                )
                if not text:
                    return
            finally:
                if not self.settings.attachments.keep_downloaded_files:
                    with suppress(OSError):
                        media_path.unlink()

        if not text:
            return
        for chunk in split_telegram_message(text):
            await send_bot_message(self.settings.telegram.bot_token, destination, chunk, parse_mode="HTML")

    async def _download_attachment(self, message: Message) -> Path | None:
        temp_dir = Path(self.settings.attachments.temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        downloaded = await message.download_media(file=str(temp_dir))
        return Path(downloaded) if downloaded else None

    def _configure_knowledge_reload_job(self) -> None:
        with suppress(Exception):
            self.scheduler.scheduler.remove_job("knowledge-reload")
        settings = self.settings.reply_suggestions
        if not settings.knowledge.enabled or not settings.knowledge.paths:
            return
        self.scheduler.scheduler.add_job(
            self.reload_knowledge,
            "interval",
            seconds=settings.reload_interval_seconds,
            id="knowledge-reload",
            replace_existing=True,
        )

    async def reload_knowledge(self):
        return await self.knowledge_cache.reload()

    def _schedule_config_reload(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._reload_config_from_watcher()))

    async def _reload_config_from_watcher(self) -> None:
        result = await self.reload_config()
        await self._send_to_destination(self.settings.telegram.send_translations_to_chat_id, result)

    async def reload_config(self) -> str:
        new_settings = load_settings(self.config_path)
        if new_settings.mongodb.uri != self.settings.mongodb.uri:
            return "Config reload skipped: MongoDB URI changed and requires restart."
        if (
            new_settings.telegram.api_id != self.settings.telegram.api_id
            or new_settings.telegram.api_hash != self.settings.telegram.api_hash
            or new_settings.telegram.session_name != self.settings.telegram.session_name
            or new_settings.telegram.bot_session_name != self.settings.telegram.bot_session_name
            or new_settings.telegram.bot_token != self.settings.telegram.bot_token
        ):
            return "Config reload skipped: Telegram credentials/session changed and require restart."

        self.settings = new_settings
        self.ai = OpenAIService(new_settings)
        self.storage.settings = new_settings
        self.knowledge_cache = KnowledgeCache(new_settings.reply_suggestions.knowledge)
        self.scheduler.settings = new_settings
        self.scheduler.configure(
            hourly_job=self.send_hourly_summaries,
            daily_job=self.send_daily_summaries,
            prune_job=self.storage.prune_old_messages,
        )
        self._configure_knowledge_reload_job()
        await self.reload_knowledge()
        return "Config reloaded successfully."

    async def _cmd_help(self, args: list[str], message: Message) -> str:
        return (
            "/list_chats\n/reload_config\n/config_status\n/test_send\n"
            "/summary [all|chat_id]\n/translate_last [count]|[chat_id count]\n"
            "/mute <chat_id>\n/unmute <chat_id>\n/enable <chat_id>\n/disable <chat_id>\n"
            "/reply_suggestions [on|off] or /reply_suggestions <chat_id> on|off|inherit\n"
            "/reply_count [1-5]\n/reload_knowledge\n/knowledge_status\n"
            "/important_only <chat_id> on|off\n/ignored_users\n/ignore_user <user_id>\n"
            "/unignore_user <user_id>\n/ignored_chats\n/ignore_chat <chat_id>\n/unignore_chat <chat_id>"
        )

    async def _cmd_list_chats(self, args: list[str], message: Message) -> str:
        rows = await list_dialogs(self.client)
        return "\n".join(rows[:80])

    async def _cmd_reload_config(self, args: list[str], message: Message) -> str:
        return await self.reload_config()

    async def _cmd_config_status(self, args: list[str], message: Message) -> str:
        watched = sum(1 for chat in self.settings.chats if chat.enabled)
        muted = sum(1 for chat in self.settings.chats if chat.muted)
        important_only = sum(1 for chat in self.settings.chats if chat.important_only)
        return (
            f"Watched chats: {watched}\nMuted chats: {muted}\nImportant-only chats: {important_only}\n"
            f"Hourly summary: {self.settings.features.hourly_summary}\nDaily summary: {self.settings.features.daily_summary}\n"
            f"MongoDB database: {self.settings.mongodb.database_name}"
        )

    async def _cmd_test_send(self, args: list[str], message: Message) -> str:
        await self._send_to_destination(self.settings.telegram.send_translations_to_chat_id, "Test translation destination OK.")
        await self._send_to_destination(self.settings.telegram.send_summaries_to_chat_id, "Test summary destination OK.")
        return "Sent test messages."

    async def _cmd_summary(self, args: list[str], message: Message) -> str:
        if args and args[0].lower() == "all":
            messages = await self.storage.recent_messages(since=datetime.now(UTC) - timedelta(hours=24), limit=500)
            title = "Manual Combined Summary"
        else:
            chat_id = int(args[0]) if args else int(message.chat_id or 0)
            if args or self.settings.chat_for(chat_id):
                messages = await self.storage.recent_messages(
                    chat_id=chat_id,
                    since=datetime.now(UTC) - timedelta(hours=24),
                    limit=250,
                )
                title = f"Manual Summary: {chat_id}"
            else:
                messages = await self.storage.recent_messages(since=datetime.now(UTC) - timedelta(hours=24), limit=500)
                title = "Manual Combined Summary"
        if not messages:
            return "No recent stored messages found."
        chat_memories = await self.storage.get_chat_memories(list({int(row["chat_id"]) for row in messages}))
        payload = [_message_for_ai(row, chat_memories) for row in messages]
        return await self.ai.summarize(payload, title=title)

    async def _cmd_translate_last(self, args: list[str], message: Message) -> str:
        if len(args) == 0:
            chat_id, count = int(message.chat_id or 0), 10
            if not self.settings.chat_for(chat_id):
                return "Usage from bot chat: /translate_last <watched_chat_id> <count>"
        elif len(args) == 1:
            chat_id, count = int(message.chat_id or 0), int(args[0])
            if not self.settings.chat_for(chat_id):
                return "Usage from bot chat: /translate_last <watched_chat_id> <count>"
        else:
            chat_id, count = int(args[0]), int(args[1])
        rows = await self.storage.last_messages_for_chat(chat_id, min(count, 50))
        if not rows:
            return "No stored messages found for that chat."
        chat_memory = await self.storage.get_chat_memory(chat_id)
        payload = [_message_for_ai(row, {chat_id: chat_memory} if chat_memory else {}) for row in rows if row.get("contains_chinese")]
        if not payload:
            return "No Chinese messages found in that range."
        return await self.ai.translate_batch(payload)

    async def _cmd_mute(self, args: list[str], message: Message) -> str:
        chat = self._require_chat(args)
        chat.muted = True
        return f"Muted {chat.id}."

    async def _cmd_unmute(self, args: list[str], message: Message) -> str:
        chat = self._require_chat(args)
        chat.muted = False
        return f"Unmuted {chat.id}."

    async def _cmd_enable(self, args: list[str], message: Message) -> str:
        chat = self._get_or_add_chat(int(args[0]))
        chat.enabled = True
        return f"Enabled {chat.id}."

    async def _cmd_disable(self, args: list[str], message: Message) -> str:
        chat = self._require_chat(args)
        chat.enabled = False
        return f"Disabled {chat.id}."

    async def _cmd_important_only(self, args: list[str], message: Message) -> str:
        if len(args) < 2:
            return "Usage: /important_only <chat_id> on|off"
        chat = self._get_or_add_chat(int(args[0]))
        chat.important_only = args[1].lower() == "on"
        return f"Important-only for {chat.id}: {chat.important_only}"

    async def _cmd_reply_suggestions(self, args: list[str], message: Message) -> str:
        if not args:
            global_enabled = (
                self.reply_suggestions_enabled_override
                if self.reply_suggestions_enabled_override is not None
                else self.settings.reply_suggestions.enabled
            )
            overridden = [
                f"{chat_id}: {'inherit' if value is None else value}"
                for chat_id, value in sorted(self.reply_suggestions_chat_overrides.items())
            ]
            details = "\n".join(overridden) if overridden else "No runtime chat overrides."
            return f"Reply suggestions global: {global_enabled}\n{details}"

        if len(args) == 1:
            value = _parse_on_off(args[0])
            if value is None:
                return "Usage: /reply_suggestions on|off or /reply_suggestions <chat_id> on|off|inherit"
            self.reply_suggestions_enabled_override = value
            return f"Reply suggestions global: {value}"

        chat_id = int(args[0])
        action = args[1].lower()
        if action == "inherit":
            self.reply_suggestions_chat_overrides.pop(chat_id, None)
            return f"Reply suggestions for {chat_id}: inherit"
        value = _parse_on_off(action)
        if value is None:
            return "Usage: /reply_suggestions <chat_id> on|off|inherit"
        self.reply_suggestions_chat_overrides[chat_id] = value
        return f"Reply suggestions for {chat_id}: {value}"

    async def _cmd_reply_count(self, args: list[str], message: Message) -> str:
        if not args:
            return f"Reply suggestion count: {self._reply_count()}"
        count = int(args[0])
        if count < 1 or count > 5:
            return "Reply suggestion count must be between 1 and 5."
        self.reply_count_override = count
        return f"Reply suggestion count updated to {count}."

    async def _cmd_reload_knowledge(self, args: list[str], message: Message) -> str:
        status = await self.reload_knowledge()
        return _format_knowledge_status(status)

    async def _cmd_knowledge_status(self, args: list[str], message: Message) -> str:
        return _format_knowledge_status(self.knowledge_cache.status())

    async def _cmd_ignored_users(self, args: list[str], message: Message) -> str:
        return "\n".join(str(user_id) for user_id in sorted(self.settings.ignore.users)) or "No ignored users."

    async def _cmd_ignore_user(self, args: list[str], message: Message) -> str:
        self.settings.ignore.users.add(int(args[0]))
        return f"Ignoring user {args[0]}."

    async def _cmd_unignore_user(self, args: list[str], message: Message) -> str:
        self.settings.ignore.users.discard(int(args[0]))
        return f"Stopped ignoring user {args[0]}."

    async def _cmd_ignored_chats(self, args: list[str], message: Message) -> str:
        return "\n".join(str(chat_id) for chat_id in sorted(self.settings.ignore.chats)) or "No ignored chats."

    async def _cmd_ignore_chat(self, args: list[str], message: Message) -> str:
        self.settings.ignore.chats.add(int(args[0]))
        return f"Ignoring chat {args[0]}."

    async def _cmd_unignore_chat(self, args: list[str], message: Message) -> str:
        self.settings.ignore.chats.discard(int(args[0]))
        return f"Stopped ignoring chat {args[0]}."

    def _require_chat(self, args: list[str]) -> ChatSettings:
        if not args:
            raise ValueError("chat_id is required")
        chat_id = int(args[0])
        chat = self.settings.chat_for(chat_id)
        if chat is None:
            raise ValueError(f"Chat is not configured: {chat_id}")
        return chat

    def _get_or_add_chat(self, chat_id: int) -> ChatSettings:
        chat = self.settings.chat_for(chat_id)
        if chat is not None:
            return chat
        chat = ChatSettings(id=chat_id, name=str(chat_id))
        self.settings.chats.append(chat)
        return chat


async def list_dialogs(client: TelegramClient) -> list[str]:
    rows: list[str] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        rows.append(f"ID: {dialog.id} | Type: {entity.__class__.__name__} | Name: {dialog.name}")
    return rows


def _message_for_ai(message: dict[str, Any], chat_memories: dict[int, dict[str, Any]]) -> dict[str, Any]:
    chat_id = int(message["chat_id"])
    chat_memory = chat_memories.get(chat_id)
    chat_title = _chat_title_from_memory(chat_memory) or str(chat_id)
    chat_title_translation = _chat_title_translation_from_memory(chat_memory, current_title=chat_title)
    return {
        "chat": chat_title,
        "chat_translation": chat_title_translation,
        "chat_display": _chat_display_for_summary(chat_title, chat_title_translation),
        "sender": message.get("sender_name"),
        "date": message.get("date"),
        "original": message.get("text"),
        "translation": message.get("translation"),
        "important": message.get("important"),
        "alerts": message.get("alerts"),
        "attachment": message.get("attachment"),
    }


def _chat_title_from_memory(chat_memory: dict[str, Any] | None) -> str | None:
    if not chat_memory:
        return None
    title = chat_memory.get("chat_title")
    return str(title) if title else None


def _chat_title_translation_from_memory(
    chat_memory: dict[str, Any] | None,
    *,
    current_title: str | None = None,
) -> str | None:
    if not chat_memory:
        return None
    if chat_memory.get("chat_title_translation_stale"):
        return None
    if current_title and chat_memory.get("chat_title") != current_title:
        return None
    translation = chat_memory.get("chat_title_translation")
    if translation:
        return str(translation)
    return None


def _reply_chat_memory_payload(chat_memory: dict[str, Any] | None) -> dict[str, Any]:
    if not chat_memory:
        return {}
    return {
        "chat_title": chat_memory.get("chat_title"),
        "chat_title_translation": chat_memory.get("chat_title_translation"),
        "memory": chat_memory.get("memory", {}),
    }


def _parse_on_off(value: str) -> bool | None:
    normalized = value.lower()
    if normalized in {"on", "true", "yes", "1"}:
        return True
    if normalized in {"off", "false", "no", "0"}:
        return False
    return None


def _format_knowledge_status(status: KnowledgeStatus) -> str:
    loaded_at = status.last_loaded_at.isoformat() if status.last_loaded_at else "never"
    paths = "\n".join(status.paths) if status.paths else "No paths configured."
    return (
        f"Knowledge enabled: {status.enabled}\n"
        f"Files loaded: {status.file_count}\n"
        f"Characters loaded: {status.char_count}\n"
        f"Last loaded: {loaded_at}\n"
        f"Paths:\n{paths}"
    )


def _attachment_metadata(message: Message) -> dict[str, Any] | None:
    media_type = _displayable_media_type(message)
    file = getattr(message, "file", None)
    if not media_type or not file:
        return None
    return {
        "type": media_type,
        "name": getattr(file, "name", None),
        "mime_type": getattr(file, "mime_type", None),
        "size": getattr(file, "size", None),
    }


def _displayable_media_type(message: Message) -> str | None:
    if not getattr(message, "media", None):
        return None
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "video_note", None):
        return "video_note"
    if getattr(message, "gif", None):
        return "animation"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "audio", None):
        return "audio"

    file = getattr(message, "file", None)
    mime_type = getattr(file, "mime_type", None) if file else None
    if mime_type == "image/gif":
        return "animation"
    if mime_type and mime_type.startswith("image/"):
        return "photo"
    if mime_type and mime_type.startswith("video/"):
        return "video"
    if mime_type and mime_type.startswith("audio/"):
        return "audio"
    if file:
        return "document"
    return None


def _media_within_size_limit(message: Message, max_mb: int) -> bool:
    if max_mb <= 0:
        return True
    file = getattr(message, "file", None)
    size = getattr(file, "size", None) if file else None
    if size is None:
        return True
    return int(size) <= max_mb * 1024 * 1024


def _media_supports_caption(media_type: str) -> bool:
    return media_type not in {"sticker", "video_note"}


def _chat_display_for_summary(chat_title: str, chat_title_translation: Any) -> str:
    if not chat_title_translation or str(chat_title_translation).casefold() == chat_title.casefold():
        return f"`{chat_title}`"
    return f"`{chat_title}` / `{chat_title_translation}`"


def _protected_ranges_from_entities(text: str, entities: list[Any]) -> list[ProtectedRange]:
    ranges: list[ProtectedRange] = []
    for entity in entities:
        if not isinstance(entity, (MessageEntityPre, MessageEntityCode)):
            continue
        start = _utf16_offset_to_index(text, int(entity.offset))
        end = _utf16_offset_to_index(text, int(entity.offset + entity.length))
        kind = "code_block" if isinstance(entity, MessageEntityPre) else "inline_code"
        language = getattr(entity, "language", None) if kind == "code_block" else None
        if kind == "code_block" and not text[start:end].strip() and language:
            recovered_start = start
            recovered_end = end
            if start > 0 and text[start - 1] == "\n":
                recovered_start = start - 1
            ranges.append(
                ProtectedRange(
                    kind="inline_code",
                    start=recovered_start,
                    end=recovered_end,
                    language=str(language),
                )
            )
            continue
        if start >= end:
            continue
        ranges.append(ProtectedRange(kind=kind, start=start, end=end, language=language or None))
    return ranges


def _utf16_offset_to_index(text: str, offset: int) -> int:
    units = 0
    for index, character in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
    return len(text)


def _important_reason(reason: str | None, alerts: dict[str, bool]) -> str | None:
    if reason:
        return reason
    active_alerts = []
    if alerts.get("name_mention"):
        active_alerts.append("Name mention")
    if alerts.get("question_or_request"):
        active_alerts.append("Question/request")
    if alerts.get("urgent"):
        active_alerts.append("Urgent")
    if not active_alerts:
        return None
    return ", ".join(active_alerts)
