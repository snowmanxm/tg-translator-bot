# Telegram Translator Bot

Private Telegram user-client bot that watches selected Chinese chats, translates incoming messages to English, and sends periodic summaries.

## Setup

```bash
source .venv/bin/activate
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` and `config.yaml`, then validate:

```bash
python -m translator_bot validate-config
```

Log in and list chats:

```bash
python -m translator_bot list-chats
```

Copy chat IDs into `config.yaml`, then run:

```bash
python -m translator_bot run
```

## Useful Commands

Send these commands from your own Telegram account, preferably in your private control chat:

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
