from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from telethon import TelegramClient

from translator_bot.config import load_settings
from translator_bot.telegram_app import TelegramTranslatorApp, list_dialogs


app = typer.Typer(help="Telegram Chinese-to-English translator bot.")
console = Console()


@app.command()
def run(config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config YAML.")) -> None:
    """Run the Telegram translator bot."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(config)
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
    """Log in to Telegram and print dialog IDs for config.yaml."""
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
    """Send test messages to the translation and summary destinations."""
    settings = load_settings(config)

    async def _run() -> None:
        client = TelegramClient(settings.telegram.session_name, settings.telegram.api_id, settings.telegram.api_hash)
        await client.start()
        try:
            await client.send_message(settings.telegram.send_translations_to, "Test translation destination OK.")
            await client.send_message(settings.telegram.send_summaries_to_chat_id, "Test summary destination OK.")
            console.print("[green]Test messages sent.[/green]")
        finally:
            await client.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
