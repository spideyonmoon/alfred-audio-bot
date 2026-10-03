# Alfred 🎧

**Audio Forensics Telegram Bot** — powered by Pyrofork, FFmpeg, SoX, and MediaInfo.

---

## Commands

| Command | Description |
|---|---|
| `/fs` | Full forensic report (spectrogram + text + authenticity assessment) |
| `/fs -spec` | Spectrogram only |
| `/fs -info` | Text info + assessment, no spectrogram |
| `/fs -na` | Spectrogram + info, no assessment |
| `/fs -nas` | Text info only, no spectrogram, no assessment |
| `/cue` | Split a CUE+Audio album into individual tracks |
| `/cnv [format]` | Convert audio to another format (interactive menu) |
| `/log` | Verify an EAC/XLD log and identify possible CD releases |
| `/stats` | Bot status, queue depth, total analyses |
| `/help` | Full command reference |

---

## VPS setup

Copy the example configuration and edit `.env`:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set `BOT_TOKEN` from [BotFather](https://t.me/BotFather) and `API_ID` / `API_HASH`
from [my.telegram.org](https://my.telegram.org). The bot loads `.env` beside `bot.py`
before reading any settings. Values in that file take precedence over shell variables.

Access uses plain comma-separated IDs, with no JSON or topic configuration:

```dotenv
ADMIN_IDS=123456789,987654321
ALLOWED_CHATS=-1001234567890,-1009876543210
```

Admins may use the bot in any chat. Other users may use it in an allowed chat,
including any topics it has. Blank lists grant no access; with both blank, nobody
can use privileged commands. To allow a private conversation, add its user/chat ID
or make that user an admin. Replace the old JSON values when migrating and remove
`ALLOWED_TOPICS`, `SESSION_IN_MEMORY`, `HEALTH_SERVER`, and `HEALTH_PORT` from `.env`.

`TELEGRAPH_TOKEN` is optional. `MAX_CONCURRENT_JOBS` defaults to 1; each analysis can
use roughly 0.5 GB of RAM, so increase it according to available VPS memory.
`AF_EXTRACTORS` defaults to 4 and `PROGRESS_UPDATE_INTERVAL` to 8 seconds.
Invalid access lists or bot tuning values stop startup with the setting's name.

### Docker Compose

With Docker and Compose installed on the VPS:

```bash
docker compose up -d --build
docker compose logs -f alfred
```

Compose reads `.env`, restarts the bot after crashes or host reboots, and stores the
Telegram session in the `alfred-data` volume. No HTTP server or exposed port is needed.
After editing `.env`, run `docker compose up -d --force-recreate` to apply the settings.
Rebuild after code changes with `docker compose up -d --build`.
The Docker build excludes `.env` and session files.

### Run directly

Install Python 3.11+ and the system tools, then run from the repository:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg sox mediainfo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

The session is saved under `data/` beside `bot.py`; the process must be able to write
there. Restart the process after editing `.env`. For a continuously running direct
installation, use a systemd service with the virtualenv Python and this repository
as its working directory. Startup failures exit with a traceback in the process logs.
