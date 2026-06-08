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

# Containerized (matches HuggingFace Spaces deployment)
docker compose up --build
```

`af2.py` is **dual-purpose**: it is both the analysis library imported by `bot.py` (`build_report`, `generate_spectrogram`, etc.) and a self-contained CLI with its own `main()`. When changing forensic logic, test it directly via the CLI — far faster than round-tripping through Telegram.

### Environment

`.env` (gitignored) must define: `BOT_TOKEN`, `API_ID`, `API_HASH`. Optional: `TELEGRAPH_TOKEN` (forensic text reports are uploaded to telegra.ph), `ALLOWED_CHATS`, `ALLOWED_TOPICS`, `ADMIN_IDS` (all JSON — see README for the `ALLOWED_CHATS` topic-id format).

The bot detects HuggingFace Spaces via the `SPACE_ID` env var. On HF it runs the Pyrogram session **in-memory** (`:memory:`, isolating auth keys from local `.session` files) and starts a health HTTP server on port **7860** *before* connecting to Telegram so HF startup probes pass immediately. Locally it persists to `alfred_session.session`.

## Architecture

### Central queue, four workers (`bot.py`)

Every long-running command (`/fs`, `/cnv`, `/cue`) is funneled through **one shared `asyncio.Queue`** drained by `MAX_CONCURRENT_JOBS` (4) `_queue_worker()` coroutines spawned in `_on_start()`. Command handlers do **not** do work directly — they build a **job dict** and call `enqueue_universal_task(job, ctx)`. The worker dispatches on `job["type"]` (`"fs"` / `"cnv"` / `"cue"`) to the matching runner (`_run_forensic_job`, `convert._run_convert_job`, `cue_split._run_cue_job`).

Key invariants when touching the queue path:
- A job dict carries `type`, `user_id`, `filename`, and a per-type `payload`/fields. `enqueue_universal_task` stamps it with a 6-char `job_id`, registers it in the global `_active_jobs` map, enforces `MAX_QUEUE_PER_USER` (5), and attaches the `status_msg` to edit.
- `_active_jobs[job_id]` is the **single source of truth for live telemetry** (progress %, speed, ETA, status string). `/stats` renders it; `progress_callback` (in `utils.py`) writes into it.
- Cancellation: `/c_<job_id>` (or `/cancel_<job_id>`) calls `task.cancel()` on the stored `async_task`. `run_async_subprocess` in `utils.py` exists specifically to propagate `CancelledError` into child ffmpeg/sox processes (it `proc.kill()`s on cancel) — use it, not bare `subprocess`, for cancellable work.

### Cross-module telemetry coupling (important + fragile)

`utils.progress_callback` reaches back into the bot's job map via `sys.modules["bot"]._active_jobs` rather than an import — a deliberate circular-import dodge. If you rename `_active_jobs` or move it out of `bot.py`, this silently stops updating `/stats`. The lookup is wrapped in a bare `try/except`, so breakage is invisible.

### The three domain modules

Each owns its own conversational state and exposes async handlers that `bot.py` wires to Pyrogram decorators. They return a **job dict** (or `None`); `bot.py` enqueues whatever dict comes back.

- **`af2.py`** — the forensic engine. Dataclass report model (`ForensicReport` → `AudioTags`, `AudioTechnical`, `LoudnessProfile`, `AuthenticityReport`, `SpectralAnalysis`). `build_report()` orchestrates extractors that shell out to mediainfo (tags/technical), sox `stat` (acoustic measurements), and ffmpeg filters (`astats`, `ebur128`, `drmeter`, `aphasemeter`, `silencedetect`). The `SpectralEngine` class decodes audio to a numpy float array via ffmpeg pipe and runs an FFT-based **scoring system**: it accumulates a `lossy_score` and a `natural_score` from independent heuristics (HF cutoff, cliff sharpness, banding, side-channel anomaly, noise floor above cutoff, entropy, DSD detection), nets them, and maps the net to a verdict label. `make_telegraph_content` in `bot.py` and `print_report`/`print_batch_summary` in `af2.py` are two separate renderers over the same `ForensicReport`.
- **`convert.py`** — interactive transcode wizard. Inline-keyboard callback data is positional and colon-delimited: `cv:{chat_id}:{msg_id}:{format}:{mode}:{grade}` (see the module docstring). State keyed in `_convert_sessions`. `_build_ffmpeg_args` maps (format, mode, grade, samplerate) → ffmpeg flags; `_transfer_tags` copies metadata across via mutagen.
- **`cue_split.py`** — CUE-sheet album splitter implemented as a **per-user state machine** in `CUE_WAITING_LIST` (`user_id → state`). Flow: `/cue` on an audio reply kicks off a background download immediately, then the bot prompts for a `.cue` file and optional cover art via follow-up document uploads. The `cue_interceptor` handler in `bot.py` (`filters.document | filters.photo`) catches those uploads and feeds them to `check_and_process_cue_upload`, which advances the state machine and eventually returns the split job.

### Auth gate

`_check_auth(message)` runs at the top of every privileged command. `ADMIN_IDS` bypass all checks; everyone else must be in an allowed (chat, topic) pair. Telegram supergroup **topic threads** matter throughout — replies must route back to `message_thread_id`, which is why the code uses native `message.reply*` wrappers rather than raw `client.send_*` (regressing this breaks topic routing, per git history).

## Conventions worth matching

- **Telegram replies use HTML parse mode** (set globally on the `Client`), not Markdown. Use `<b>`, `<code>`, `<blockquote>`, etc.
- Wrap user-facing edits/deletes in `safe_edit` / `safe_delete` (utils) — they swallow `FloodWait` and stale-message errors.
- All temp files live under `/tmp` (e.g. `/tmp/downloads/`); jobs clean up downloaded media and generated spectrograms in a `finally` block. The crash handler writes tracebacks to `/tmp/crash.log`, served at the health endpoint `/crash`.
- Document-format validation is intentionally strict `.endswith(ext)` checks because Telegram strips extensions from native audio buffers — don't loosen these without understanding the supergroup/document-upload edge cases the git log documents.
- Throttle: `progress_callback` only edits the status message every 3s to avoid FloodWait.
