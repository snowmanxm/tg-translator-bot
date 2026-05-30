# Telegram Translator Bot

Private Telegram translator that watches selected Chinese chats from user account A, translates incoming messages, and sends translations/summaries to user B through a Telegram bot.

## Setup

```bash
source .venv/bin/activate
cp .env.example .env
cp config.yaml.example config.yaml
```

Edit `.env` and `config.yaml`, then validate:

```bash
python -m translator_bot validate-config
```

Log in as watcher account A and list chats to watch:

```bash
python -m translator_bot list-chats
```

User B must start your Telegram bot first by sending `/start` or any message. Then list Bot API updates to find user B's bot chat ID:

```bash
python -m translator_bot list-bot-chats
```

Copy watcher chat IDs and bot destination chat IDs into `config.yaml`, then test bot delivery and run:

```bash
python -m translator_bot test-send
python -m translator_bot run
```

## Useful Commands

Send these commands to the Telegram bot from user B's account or configured `control_chat_id`:

- `/list-chats`
- `/reload-config`
- `/config-status`
- `/test-send`
- `/summary`
- `/summary all`
- `/summary <chat_id>`
- `/translate-last`
- `/translate-last <count>`
- `/translate-last <chat_id> <count>`
- `/mute <chat_id>`
- `/unmute <chat_id>`
- `/enable <chat_id>`
- `/disable <chat_id>`
- `/important-only <chat_id> on`
- `/important-only <chat_id> off`
- `/ignored-users`
- `/ignore-user <user_id>`
- `/unignore-user <user_id>`
- `/ignored-chats`
- `/ignore-chat <chat_id>`
- `/unignore-chat <chat_id>`
- `/help`
