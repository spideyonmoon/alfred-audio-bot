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

## Secrets required (Space Settings → Variables and Secrets)

| Secret | Description |
|---|---|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAPH_TOKEN` | Telegraph access token (optional) |
| `ALLOWED_CHATS` | JSON: `{"-100chatid": [0, topic_id]}` |
| `ALLOWED_TOPICS` | JSON: `[topic_id1, topic_id2]` |

---

## ALLOWED_CHATS Format

```json
{"-1001234567890": [0, 123, 456]}
```

- Use `0` for the general (non-topic) chat
- Topic IDs: forward a message from the topic to @userinfobot
