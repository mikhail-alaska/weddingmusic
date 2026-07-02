# weddingmusic

Telegram bot for collecting wedding music requests.

## Configuration

Create `.env` in this directory:

```env
TG_BOT_TOKEN=123456:telegram-token
WEDDING_GROUP_CHAT_ID=-1001234567890
# Optional:
# PROXY_URL=http://127.0.0.1:2080
```

The bot must be added to the target Telegram group. `WEDDING_GROUP_CHAT_ID` is the group chat id where confirmed tracks are sent.

## Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

The bot uses `yt-dlp` from the parent directory when `../yt-dlp` exists, otherwise it uses `yt-dlp` from `PATH`.
