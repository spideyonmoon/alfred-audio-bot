---
title: Alfrerd
emoji: 🔥
colorFrom: yellow
colorTo: yellow
sdk: docker
pinned: false
license: mit
short_description: Alfred — Audio Forensics Telegram Bot
app_port: 7860
---

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
| `/stats` | Bot status, queue depth, total analyses |
| `/help` | Full command reference |

---

## Deployment

The same image runs on **HuggingFace Spaces** and on **Render** (Docker service). The bot
detects the host itself — no config needed for the port, which comes from `$PORT` on Render
and defaults to `7860` on HuggingFace.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_JOBS` | `1` | Concurrent analyses. Each peaks near 0.5 GB, so raise only with the RAM to back it. |
| `HEALTH_PORT` | `$PORT`, else `7860` | Override the health-probe port. |
| `HEALTH_SERVER` | `1` | `0` to disable the health endpoint (local runs). |
| `SESSION_IN_MEMORY` | auto | `1`/`0` to force the session in memory or on disk. |

**Render free tier:** the service sleeps after ~15 minutes without inbound HTTP traffic, and
a Telegram connection doesn't count as HTTP. Point a free uptime pinger (UptimeRobot,
cron-job.org, a GitHub Actions schedule) at the service URL every 5–10 minutes to keep it
awake. The health endpoint answers `200` on any path; `/crash` serves the last startup
traceback.

**Memory:** a full-length analysis needs roughly 0.5 GB *inside the bot process*, plus the
ffmpeg/sox children. That fits the free 512 MB instance only for shorter tracks — see
`CLAUDE.md` for the details before raising `MAX_CONCURRENT_JOBS` or accepting longer files.

---

## Secrets required (Space Settings → Variables and Secrets on HuggingFace; Environment on Render)

| Secret | Description |
|---|---|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAPH_TOKEN` | Telegraph access token (optional) |
| `ALLOWED_CHATS` | JSON: `{"-100chatid": [0, topic_id]}` |
| `ALLOWED_TOPICS` | JSON: `[topic_id1, topic_id2]` |
| `ADMIN_IDS` | JSON: `[userid1, userid2]` (these users bypass chat checks) |

---

## ALLOWED_CHATS Format

```json
{"-1001234567890": [0, 123, 456]}
```

- Use `0` for the general (non-topic) chat
- Topic IDs: forward a message from the topic to @userinfobot
