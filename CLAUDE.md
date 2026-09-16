# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Alfred is an **audio forensics Telegram bot**. Users reply to an audio file with a command and Alfred analyzes authenticity (lossy-vs-lossless detection), loudness/dynamics, spectral integrity, splits CUE+audio albums into tagged tracks, and transcodes between formats. It wraps three external CLI binaries — **ffmpeg, sox, mediainfo** — plus a numpy FFT engine, behind the Pyrofork (Pyrogram fork) MTProto client.

## Running & deploying

There is **no build step and no test suite**. It's pure Python 3.11 + three system binaries.

```bash
# Run the bot locally (needs .env populated — see below)
python bot.py

# Run the forensic engine standalone as a CLI (no Telegram involved)
python af2.py track.flac                 # full pretty terminal report
python af2.py *.flac                      # multiple files → per-file reports + album batch summary
python af2.py track.flac --json           # machine-readable JSON
python af2.py track.flac --fast           # analyse first 60s only
python af2.py track.flac --info           # metadata only, skip DSP

# Containerized (the same image runs on HuggingFace Spaces and Render)
docker compose up --build
```

`af2.py` is **dual-purpose**: it is both the analysis library imported by `bot.py` (`build_report`, `generate_spectrogram`, etc.) and a self-contained CLI with its own `main()`. When changing forensic logic, test it directly via the CLI — far faster than round-tripping through Telegram.

`af2.py` is a **vendored copy of the upstream engine** (`audio-forensic/audio_forensic.py`) plus two bot-only shims near the bottom of the file (`compare_reports`, `comparison_to_dict`). To sync with upstream, copy the engine in wholesale and re-add that block — don't hand-merge individual functions, or the diff against upstream stops being reviewable.

### Environment

`.env` (gitignored) must define: `BOT_TOKEN`, `API_ID`, `API_HASH`. Optional: `TELEGRAPH_TOKEN` (forensic text reports are uploaded to telegra.ph), `ALLOWED_CHATS`, `ALLOWED_TOPICS`, `ADMIN_IDS` (all JSON — see README for the `ALLOWED_CHATS` topic-id format).

Deployment tuning knobs (all optional, defaults are safe for a small instance):

| Variable | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_JOBS` | `1` | Concurrent analyses. Each holds ~0.5 GB, so raise this only on a host with the RAM. |
| `HEALTH_PORT` | `$PORT`, else `7860` | Overrides the health/port probe port. |
| `HEALTH_SERVER` | `1` | Set `0` to skip the health server (local runs). |
| `SESSION_IN_MEMORY` | auto | `1`/`0` to force the Pyrogram session in memory or on disk. |

### Hosting

The bot runs on HuggingFace Spaces and on Render, and detects which (`_detect_platform()` in `bot.py`) from `SPACE_ID` / `RENDER_SERVICE_ID`. Both give the container an **ephemeral filesystem**, so the Pyrogram session is kept in memory (`:memory:`) rather than written to a `.session` file that would be wiped on every redeploy. Locally it persists to `alfred_session.session`.

The health HTTP server starts *before* the MTProto connection is negotiated, so the platform's port probe passes immediately. It answers 200 on every path (`/` and `/health` alike) and serves the crash traceback at `/crash`. The port comes from `health.resolve_port()`: `$PORT` (Render's convention) → `HEALTH_PORT` → 7860 (HuggingFace's convention).

**Render free-tier spin-down:** free web services sleep after ~15 minutes without *inbound HTTP* traffic, and Telegram's MTProto connection does not count as HTTP traffic. Keep it awake with an external pinger (UptimeRobot, cron-job.org, a GitHub Actions cron) hitting the health URL every 5–10 minutes; the endpoint is deliberately cheap so this costs nothing.

**Memory is the binding constraint on a small instance.** One full-length analysis peaks around 450-500 MB resident (measured on a 7-minute FLAC) *inside the bot process*, plus ffmpeg/sox children. Render's free tier gives 512 MB total, so a single long track can still get the container OOM-killed — see the queue section below before raising concurrency or accepting longer files.

## Architecture

### Central queue, `MAX_CONCURRENT_JOBS` workers (`bot.py`)

Every long-running command (`/fs`, `/cnv`, `/cue`) is funneled through **one shared `asyncio.Queue`** drained by `MAX_CONCURRENT_JOBS` `_queue_worker()` coroutines spawned in `_on_start()`. That count defaults to **1** (env-overridable): `build_report()` runs *in the bot's own process* via `asyncio.to_thread`, holding the decoded track plus its STFT — roughly 0.5 GB for a full-length track. Four workers want ~2 GB and get the container OOM-killed on a 512 MB instance, so the queue serialises the wait instead. Raise the variable only alongside the RAM. Command handlers do **not** do work directly — they build a **job dict** and call `enqueue_universal_task(job, ctx)`. The worker dispatches on `job["type"]` (`"fs"` / `"cnv"` / `"cue"`) to the matching runner (`_run_forensic_job`, `convert._run_convert_job`, `cue_split._run_cue_job`).

Key invariants when touching the queue path:
- A job dict carries `type`, `user_id`, `filename`, and a per-type `payload`/fields. `enqueue_universal_task` stamps it with a 6-char `job_id`, registers it in the global `_active_jobs` map, enforces `MAX_QUEUE_PER_USER` (5), and attaches the `status_msg` to edit.
- `_active_jobs[job_id]` is the **single source of truth for live telemetry** (progress %, speed, ETA, status string). `/stats` renders it; `progress_callback` (in `utils.py`) writes into it.
- Cancellation: `/c_<job_id>` (or `/cancel_<job_id>`) calls `task.cancel()` on the stored `async_task`. `run_async_subprocess` in `utils.py` exists specifically to propagate `CancelledError` into child ffmpeg/sox processes (it `proc.kill()`s on cancel) — use it, not bare `subprocess`, for cancellable work.

### Cross-module telemetry coupling (important + fragile)

`utils.progress_callback` reaches back into the bot's job map via `sys.modules["bot"]._active_jobs` rather than an import — a deliberate circular-import dodge. If you rename `_active_jobs` or move it out of `bot.py`, this silently stops updating `/stats`. The lookup is wrapped in a bare `try/except`, so breakage is invisible.

### The three domain modules

Each owns its own conversational state and exposes async handlers that `bot.py` wires to Pyrogram decorators. They return a **job dict** (or `None`); `bot.py` enqueues whatever dict comes back.

- **`af2.py`** — the forensic engine. Dataclass report model (`ForensicReport` → `AudioTags`, `AudioTechnical`, `LoudnessProfile`, `AuthenticityReport`, `SpectralAnalysis`). `build_report()` orchestrates extractors that shell out to mediainfo (tags/technical), sox `stat` (acoustic measurements), and a single ffmpeg graph (`astats`, `ebur128`, `drmeter` split from one decode). Phase correlation, clipping counts, silence mapping and the fallback noise floor are **byproducts of the engine's own decode** rather than extra ffmpeg filter passes, so they cost no additional subprocess. The `SpectralEngine` class decodes audio to a numpy float array via ffmpeg pipe and runs an FFT-based **scoring system**: it accumulates a `lossy_score` and a `natural_score` from independent heuristics (HF cutoff, cliff sharpness, banding, side-channel anomaly, noise floor above cutoff, entropy, DSD detection), nets them, and maps the net to a verdict label. The scipy-backed advanced suite adds a **MDCT quantization-error detector** (Derrien, JAES 2019; `_mdct_quant_error`): it re-applies AAC's MDCT + scalefactor quantization to the decoded signal and counts scalefactor bands whose rounding error collapses to near-zero — the fingerprint of an AAC encoder's quantizer surviving in "lossless" PCM. It is the backstop for high-bitrate AAC transcodes that keep full bandwidth and so leave **no lowpass wall** for the cutoff/void/fingerprint rules to catch. Only meaningful at 44.1/48 kHz (the scalefactor-band table is rate-specific); returns `-1` (n/a) otherwise. Two further structural detectors sit alongside it: `_vorbis_grid` (persistent near-zero MDCT coefficients on Vorbis long-block alignments) and `inspect_mqa` (the embedded 36-bit MQA sync word, read from the decoded PCM rather than the tags). Bit-depth authenticity is a **two-prong** check (`check_bit_depth_authenticity`): trailing-zero used-bits analysis plus a noise-floor/effective-dynamic-range prong, so dithered upscales are no longer reported as "verified" hi-res. `make_telegraph_content` in `bot.py` and `print_report`/`print_batch_summary` in `af2.py` are two separate renderers over the same `ForensicReport`.
- **`/log` (inline in `bot.py`)** — EAC/XLD rip log checker. Replies to a `.log` document, POSTs it to `https://logcheck.nirzak.win/api` (multipart `logfile` field) via httpx, and formats the JSON response (`score`, `ripper`, `ripper_version`, `checksum_state`, `details[]`). Intentionally not queued — no heavy processing.
- **`convert.py`** — interactive transcode wizard. Inline-keyboard callback data is positional and colon-delimited: `cv:{chat_id}:{msg_id}:{format}:{mode}:{grade}` (see the module docstring). State keyed in `_convert_sessions`. `_build_ffmpeg_args` maps (format, mode, grade, samplerate) → ffmpeg flags; `_transfer_tags` copies metadata across via mutagen.
- **`cue_split.py`** — CUE-sheet album splitter implemented as a **per-user state machine** in `CUE_WAITING_LIST` (`user_id → state`). Flow: `/cue` on an audio reply kicks off a background download immediately, then the bot prompts for a `.cue` file and optional cover art via follow-up document uploads. The `cue_interceptor` handler in `bot.py` (`filters.document | filters.photo`) catches those uploads and feeds them to `check_and_process_cue_upload`, which advances the state machine and eventually returns the split job.

### Auth gate

`_check_auth(message)` runs at the top of every privileged command. `ADMIN_IDS` bypass all checks; everyone else must be in an allowed (chat, topic) pair. Telegram supergroup **topic threads** matter throughout — but the rule is precise:

- **`client.send_*(chat_id=..., message_thread_id=thread_id, ...)`** — must pass `message_thread_id` explicitly, no implicit context.
- **`message.reply_*(...)` / `message.reply_audio(...)` etc.** — must NOT pass `message_thread_id`. Pyrogram derives the thread from the reply anchor automatically, and the kwarg is not accepted by these methods (raises `TypeError` at runtime).

## Conventions worth matching

- **Telegram replies use HTML parse mode** (set globally on the `Client`), not Markdown. Use `<b>`, `<code>`, `<blockquote>`, etc.
- **Roadmap — Rich Messages (Bot API 10.1):** `sendRichMessage` (headings, tables, collapsible `<details>` blocks) would be a great fit for the forensic result, but Pyrofork (MTProto) has no binding for it yet and its media blocks are HTTP-URL-only (can't embed the local spectrogram). Deferred until Pyrofork ships support; until then results use ordinary HTML formatting.
- Wrap user-facing edits/deletes in `safe_edit` / `safe_delete` (utils) — they swallow `FloodWait` and stale-message errors.
- All temp files live under `/tmp` (e.g. `/tmp/downloads/`); jobs clean up downloaded media and generated spectrograms in a `finally` block. The crash handler writes tracebacks to `/tmp/crash.log`, served at the health endpoint `/crash`.
- Document-format validation is intentionally strict `.endswith(ext)` checks because Telegram strips extensions from native audio buffers — don't loosen these without understanding the supergroup/document-upload edge cases the git log documents.
- Throttle: `progress_callback` only edits the status message every 3s to avoid FloodWait.
