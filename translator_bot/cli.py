from __future__ import annotations

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import typer
from rich.console import Console
from telethon import TelegramClient

from translator_bot.bot_api import BotApiError, fetch_bot_updates, send_bot_message_sync
from translator_bot.config import load_settings
from translator_bot.telegram_app import TelegramTranslatorApp, list_dialogs


app = typer.Typer(help="Telegram Chinese-to-English translator bot.")
console = Console()


@app.command()
def run(config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config YAML.")) -> None:
    """Run the Telegram translator bot."""
    settings = load_settings(config)
    _configure_logging(settings)
    translator = TelegramTranslatorApp(settings, config_path=config)
    asyncio.run(translator.run())


@app.command("validate-config")
def validate_config(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")) -> None:
    """Validate config and required environment variables."""
    settings = load_settings(config)
    console.print("[green]Config is valid.[/green]")
    console.print(f"MongoDB database: [bold]{settings.mongodb.database_name}[/bold]")
    console.print(f"Configured chats: [bold]{len(settings.chats)}[/bold]")


@app.command("list-chats")
def list_chats(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")) -> None:
    """Log in as watcher account A and print dialog IDs for watched chats."""
    settings = load_settings(config)

    async def _run() -> None:
        client = TelegramClient(settings.telegram.session_name, settings.telegram.api_id, settings.telegram.api_hash)
        await client.start()
        try:
            for row in await list_dialogs(client):
                console.print(row)
        finally:
            await client.disconnect()

    asyncio.run(_run())


@app.command("test-send")
def test_send(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")) -> None:
    """Send test messages through the sender bot to user B destinations."""
    settings = load_settings(config)
    try:
        send_bot_message_sync(
            settings.telegram.bot_token,
            settings.telegram.send_translations_to_chat_id,
            "Test translation destination OK.",
        )
        send_bot_message_sync(
            settings.telegram.bot_token,
            settings.telegram.send_summaries_to_chat_id,
            "Test summary destination OK.",
        )
    except BotApiError as exc:
        console.print("[red]Bot test send failed.[/red]")
        console.print(str(exc))
        console.print("Make sure user B has opened the bot and sent /start, then run:")
        console.print("[bold]python -m translator_bot list-bot-chats[/bold]")
        console.print("Use that ID for send_translations_to_chat_id and send_summaries_to_chat_id.")
        raise typer.Exit(code=1) from exc
    console.print("[green]Test messages sent.[/green]")


@app.command("list-bot-chats")
def list_bot_chats(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")) -> None:
    """Print chats that have messaged the sender bot via Bot API updates."""
    settings = load_settings(config)
    updates = fetch_bot_updates(settings.telegram.bot_token)
    chats: dict[int, dict[str, str | int | None]] = {}

    for update in updates:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            continue
        chat_id = chat["id"]
        title = chat.get("title") or " ".join(
            part for part in [chat.get("first_name"), chat.get("last_name")] if isinstance(part, str)
        )
        chats[chat_id] = {
            "id": chat_id,
            "type": chat.get("type"),
            "name": title or chat.get("username") or "",
            "username": chat.get("username"),
        }

    if not chats:
        console.print("[yellow]No bot chats found.[/yellow]")
        console.print("Open the bot as user B, send /start or any message, then run this command again.")
        return

    for chat in chats.values():
        username = f" | Username: @{chat['username']}" if chat.get("username") else ""
        console.print(f"ID: {chat['id']} | Type: {chat['type']} | Name: {chat['name']}{username}")


def _configure_logging(settings) -> None:  # type: ignore[no-untyped-def]
    log_level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    root.addHandler(console_handler)

    log_file = Path(settings.logging.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when=settings.logging.when,
        interval=1,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
        utc=settings.logging.utc,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)
    root.addHandler(file_handler)


if __name__ == "__main__":
    app()
