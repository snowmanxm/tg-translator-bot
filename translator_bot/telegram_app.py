from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from telethon.utils import get_display_name

from translator_bot.ai import OpenAIService
from translator_bot.bot_api import send_bot_message, set_bot_commands
from translator_bot.config import ChatSettings, Settings, load_settings
from translator_bot.config_watcher import ConfigWatcher
from translator_bot.formatting import format_summary, format_translation, split_telegram_message
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
        self.config_watcher: ConfigWatcher | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.chat_title_cache: dict[str, str] = {}

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.storage.setup()
        self._register_handlers()
        self.scheduler.configure(
            hourly_job=self.send_hourly_summaries,
            daily_job=self.send_daily_summaries,
            prune_job=self.storage.prune_old_messages,
        )
        self.scheduler.start()
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
        if not text:
            return

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
        chat_title_translation = self.chat_title_cache.get(chat_title)
        sender_name = get_display_name(sender) or str(sender_id)
        sender_username = getattr(sender, "username", None)
        is_chinese = contains_chinese(text)

        document = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "chat_title_translation": chat_title_translation,
            "message_id": message.id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "text": text,
            "date": message.date.astimezone(UTC) if message.date else datetime.now(UTC),
            "contains_chinese": is_chinese,
            "translation": None,
            "important": False,
            "alerts": {},
        }

        if is_chinese:
            logger.info("Analyzing Chinese message chat_id=%s message_id=%s", chat_id, message.id)
            analysis = await self.ai.analyze_message(
                text,
                chat_title=chat_title,
                sender_name=sender_name,
                known_chat_title_english=chat_title_translation,
            )
            chat_title_translation = analysis.chat_title_english
            self.chat_title_cache[chat_title] = chat_title_translation
            alerts = analysis.alerts
            important = analysis.important
            document.update(
                {
                    "chat_title_translation": chat_title_translation,
                    "translation": analysis.message_english,
                    "important": important,
                    "alerts": alerts,
                }
            )
            important_reason = _important_reason(analysis.reason, alerts) if important else None
            if important_reason:
                document["important_reason"] = important_reason
            if self._should_send_translation(chat_settings, important):
                await self._send_translation(
                    chat_title,
                    chat_title_translation,
                    sender_name,
                    sender_username,
                    text,
                    analysis.message_english,
                    important=important,
                )
                logger.info("Sent translation chat_id=%s message_id=%s", chat_id, message.id)
        else:
            logger.info("Stored non-Chinese message chat_id=%s message_id=%s", chat_id, message.id)

        await self.storage.save_message(document)

    def _should_send_translation(self, chat_settings: ChatSettings, important: bool) -> bool:
        if not self.settings.features.instant_translation:
            return False
        if not chat_settings.instant_translation or chat_settings.muted:
            return False
        if chat_settings.important_only and not important:
            return False
        return True

    async def _send_translation(
        self,
        chat_title: str,
        chat_title_translation: str | None,
        sender_name: str,
        sender_username: str | None,
        original: str,
        translation: str,
        important: bool = False,
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
        )
        await self._send_to_destination(self.settings.telegram.send_translations_to_chat_id, message)

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
            await self._summarize_and_send(messages, f"{period.title()} Combined Summary")

        if self.settings.summary.per_chat_summary:
            for chat_id in watched_ids:
                messages = await self.storage.recent_messages(chat_id=chat_id, since=since, limit=250)
                chat_name = self.settings.chat_for(chat_id).name if self.settings.chat_for(chat_id) else str(chat_id)
                chat_name_translation = _chat_title_translation_from_messages(messages)
                if chat_name_translation:
                    self.chat_title_cache[chat_name] = chat_name_translation
                await self._summarize_and_send(
                    messages,
                    f"{period.title()} Summary",
                    chat_name=chat_name,
                    chat_name_translation=chat_name_translation,
                )

        await self.storage.set_last_summary_time(key, now)

    async def _summarize_and_send(
        self,
        messages: list[dict[str, Any]],
        title: str,
        chat_name: str | None = None,
        chat_name_translation: str | None = None,
    ) -> None:
        if not messages:
            return
        payload = [_message_for_ai(message) for message in messages]
        summary = await self.ai.summarize(payload, title=title)
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
                {"command": "ignored_users", "description": "List ignored users"},
                {"command": "ignored_chats", "description": "List ignored chats"},
            ],
        )

    async def _send_to_destination(self, destination: str | int, text: str) -> None:
        if isinstance(destination, str) and destination.strip().lstrip("-").isdigit():
            destination = int(destination)
        for chunk in split_telegram_message(text):
            await send_bot_message(self.settings.telegram.bot_token, destination, chunk, parse_mode="HTML")

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
        self.scheduler.settings = new_settings
        self.scheduler.configure(
            hourly_job=self.send_hourly_summaries,
            daily_job=self.send_daily_summaries,
            prune_job=self.storage.prune_old_messages,
        )
        return "Config reloaded successfully."

    async def _cmd_help(self, args: list[str], message: Message) -> str:
        return (
            "/list_chats\n/reload_config\n/config_status\n/test_send\n"
            "/summary [all|chat_id]\n/translate_last [count]|[chat_id count]\n"
            "/mute <chat_id>\n/unmute <chat_id>\n/enable <chat_id>\n/disable <chat_id>\n"
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
        payload = [_message_for_ai(row) for row in messages]
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
        payload = [_message_for_ai(row) for row in rows if row.get("contains_chinese")]
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


def _message_for_ai(message: dict[str, Any]) -> dict[str, Any]:
    chat_title = str(message.get("chat_title") or "")
    chat_title_translation = message.get("chat_title_translation")
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
    }


def _chat_title_translation_from_messages(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        translation = message.get("chat_title_translation")
        if translation:
            return str(translation)
    return None


def _chat_display_for_summary(chat_title: str, chat_title_translation: Any) -> str:
    if not chat_title_translation or str(chat_title_translation).casefold() == chat_title.casefold():
        return f"`{chat_title}`"
    return f"`{chat_title}` / `{chat_title_translation}`"


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
