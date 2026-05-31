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

## Docker

The Docker setup keeps `.env`, `config.yaml`, and Telegram session files on the host.

Create the host session directory:

```bash
mkdir -p sessions
```

For Docker, set session paths in `config.yaml` under the mounted session directory:

```yaml
telegram:
  session_name: "/app/sessions/user_a_session"
  bot_session_name: "/app/sessions/translator_bot_session"
```

Build and run:

```bash
docker compose build
docker compose up -d
```

Run setup/helper commands through Compose:

```bash
docker compose run --rm bot validate-config
docker compose run --rm bot list-chats
docker compose run --rm bot list-bot-chats
docker compose run --rm bot test-send
```

If MongoDB runs on your host machine, use `host.docker.internal` in `.env`:

```env
MONGODB_URI=mongodb://host.docker.internal:27017/telegram_translator_bot
```

View logs:

```bash
docker compose logs -f bot
```

## Useful Commands

Send these commands to the Telegram bot from user B's account or configured `control_chat_id`:

- `/list_chats`
- `/list_bot_chats`
- `/reload_config`
- `/config_status`
- `/test_send`
- `/summary`
- `/summary all`
- `/summary <chat_id>`
- `/translate_last`
- `/translate_last <count>`
- `/translate_last <chat_id> <count>`
- `/mute <chat_id>`
- `/unmute <chat_id>`
- `/enable <chat_id>`
- `/disable <chat_id>`
- `/important_only <chat_id> on`
- `/important_only <chat_id> off`
- `/ignored_users`
- `/ignore_user <user_id>`
- `/unignore_user <user_id>`
- `/ignored_chats`
- `/ignore_chat <chat_id>`
- `/unignore_chat <chat_id>`
- `/help`
