---
title: Alfred
emoji: 🎧
colorFrom: gray
colorTo: indigo
sdk: docker
pinned: false
app_port: 7860
---

# Alfred 🎧

**Audio Forensics Telegram Bot** — powered by Pyrogram, FFmpeg, SoX, and MediaInfo.

---

## Features

| Command | Description |
|---|---|
| `/fs` | Full forensic report (spectrogram + text + authenticity assessment) |
| `/fs -spec` | Spectrogram only |
| `/fs -info` | Text info + assessment, no spectrogram |
| `/fs -na` | Spectrogram + info, no assessment |
| `/fs -nas` | Text info only, no spectrogram, no assessment |
| `/cue` | Split a CUE+Audio album into individual tracks |
| `/cnv [format]` | Convert audio to another format (interactive menu) |
| `/stats` | Bot status, queue depth, total analyses |
| `/help` | Full command reference |

---

## Local Development (Windows / PowerShell)

### Prerequisites

Install these tools and ensure they are on your PATH:
- [Python 3.11+](https://python.org)
- [FFmpeg](https://ffmpeg.org/download.html) (includes `ffprobe`)
- [SoX](https://sourceforge.net/projects/sox/) (with MP3 support)
- [MediaInfo CLI](https://mediaarea.net/en/MediaInfo/Download/Windows)

### Setup

```powershell
cd C:\Users\Bishal\Documents\bots_under_construction\alfred

# Install Python dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
copy .env.example .env
# Edit .env with your API_ID, API_HASH, BOT_TOKEN, etc.

# Run
python bot.py
```

On first run, Pyrogram will create `alfred_session.session`. Keep this file — it stores the bot session.

---

## Deployment (VPS via Docker)

### Prerequisites on the VPS
- Docker + Docker Compose (`apt install docker.io docker-compose-plugin`)

### Steps

```bash
# Upload project files to VPS (example via scp)
scp -r ./alfred user@your-vps-ip:/opt/alfred

ssh user@your-vps-ip
cd /opt/alfred

# Fill in environment variables
cp .env.example .env
nano .env

# Build and start
docker compose up -d --build

# View logs
docker compose logs -f
```

The `alfred_session.session` file is persisted via the volume mount. Never delete it without stopping the bot first, or you'll need to re-authenticate.

---

## ALLOWED_CHATS Format

`ALLOWED_CHATS` is a JSON object mapping chat IDs (as strings) to lists of allowed topic IDs:

```json
{"-1001234567890": [0, 123, 456]}
```

- Use `0` for the general (non-topic) chat.
- The chat ID of a group is typically negative (starts with `-100`).
- Topic IDs can be found by forwarding a message from the topic to @userinfobot or checking the URL.

---

## Environment Variables

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAPH_TOKEN` | Telegraph access token (optional, for report pages) |
| `ALLOWED_CHATS` | JSON map of chat IDs → allowed topic ID lists |
| `ALLOWED_TOPICS` | JSON list of globally allowed topic IDs |
