# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Alfred is an **audio forensics Telegram bot**. Users reply to an audio file with a command and Alfred analyzes authenticity (lossy-vs-lossless detection), loudness/dynamics, spectral integrity, splits CUE+audio albums into tagged tracks, and transcodes between formats. It wraps three external CLI binaries — **ffmpeg, sox, mediainfo** — plus a numpy FFT engine, behind the Pyrofork (Pyrogram fork) MTProto client.

## Running & deploying

There is **no build step**. It is pure Python 3.11 plus three system binaries; run
`python -m unittest discover -v` for the focused log-checker test suite.

```bash
# Run the bot locally (needs .env populated — see below)
python bot.py

# Run the forensic engine standalone as a CLI (no Telegram involved)
python af2.py track.flac                 # full pretty terminal report
python af2.py *.flac                      # multiple files → per-file reports + album batch summary
python af2.py track.flac --json           # machine-readable JSON
python af2.py track.flac --fast           # analyse first 60s only
python af2.py track.flac --info           # metadata only, skip DSP

# Containerized on a VPS
docker compose up --build
```

`af2.py` is **dual-purpose**: it is both the analysis library imported by `bot.py` (`build_report`, `generate_spectrogram`, etc.) and a self-contained CLI with its own `main()`. When changing forensic logic, test it directly via the CLI — far faster than round-tripping through Telegram.

`af2.py` is a **vendored copy of the upstream engine** (`audio-forensic/audio_forensic.py`) plus two bot-only shims near the bottom of the file (`compare_reports`, `comparison_to_dict`). To sync with upstream, copy the engine in wholesale and re-add that block — don't hand-merge individual functions, or the diff against upstream stops being reviewable.

### Environment

`.env` (gitignored) must define `BOT_TOKEN`, `API_ID`, and `API_HASH`.
`config.py` loads it before dependent modules, with file values taking precedence over
shell variables. Optional `TELEGRAPH_TOKEN` enables telegra.ph reports. `ALLOWED_CHATS`
and `ADMIN_IDS` are comma-separated numeric IDs; blank means no access. There is no
topic allowlist. Optional tuning: `MAX_CONCURRENT_JOBS=1`, `AF_EXTRACTORS=4`, and
`PROGRESS_UPDATE_INTERVAL=8.0`. Invalid access lists and bot tuning values fail startup.

### Hosting

Deploy on a VPS using Docker Compose or a Python virtualenv (see README).
The session persists under `data/`; Compose mounts a named volume there. The Docker
build excludes credentials and sessions. The bot has no HTTP server or hosting-platform
detection. Startup failures exit normally with a traceback for the process supervisor.
Each concurrent analysis can use roughly 0.5 GB; size the worker count for the VPS RAM.

## Architecture

### Central queue, `MAX_CONCURRENT_JOBS` workers (`bot.py`)

Every long-running command (`/fs`, `/cnv`, `/cue`) is funneled through **one shared `asyncio.Queue`** drained by `MAX_CONCURRENT_JOBS` `_queue_worker()` coroutines spawned in `_on_start()`. That count defaults to **1** (env-overridable): `build_report()` runs *in the bot's own process* via `asyncio.to_thread`, holding the decoded track plus its STFT — roughly 0.5 GB for a full-length track. Four workers can need ~2 GB, so the default queue serialises the work. Raise the variable only alongside the RAM. Command handlers do **not** do work directly — they build a **job dict** and call `enqueue_universal_task(job, ctx)`. The worker dispatches on `job["type"]` (`"fs"` / `"cnv"` / `"cue"`) to the matching runner (`_run_forensic_job`, `convert._run_convert_job`, `cue_split._run_cue_job`).

Key invariants when touching the queue path:
- A job dict carries `type`, `user_id`, `filename`, and a per-type `payload`/fields. `enqueue_universal_task` stamps it with a 6-char `job_id`, registers it in the global `_active_jobs` map, enforces `MAX_QUEUE_PER_USER` (5), and attaches the `status_msg` to edit.
- `_active_jobs[job_id]` is the **single source of truth for live telemetry** (progress %, speed, ETA, status string). `/stats` renders it; `progress_callback` (in `utils.py`) writes into it.
- Cancellation: `/c_<job_id>` (or `/cancel_<job_id>`) calls `task.cancel()` on the stored `async_task`. `run_async_subprocess` in `utils.py` exists specifically to propagate `CancelledError` into child ffmpeg/sox processes (it `proc.kill()`s on cancel) — use it, not bare `subprocess`, for cancellable work.

### Cross-module telemetry coupling (important + fragile)

`utils.progress_callback` reaches back into the bot's job map via `sys.modules["bot"]._active_jobs` rather than an import — a deliberate circular-import dodge. If you rename `_active_jobs` or move it out of `bot.py`, this silently stops updating `/stats`. The lookup is wrapped in a bare `try/except`, so breakage is invisible.

### The three domain modules

Each owns its own conversational state and exposes async handlers that `bot.py` wires to Pyrogram decorators. They return a **job dict** (or `None`); `bot.py` enqueues whatever dict comes back.

- **`af2.py`** — the forensic engine. Dataclass report model (`ForensicReport` → `AudioTags`, `AudioTechnical`, `LoudnessProfile`, `AuthenticityReport`, `SpectralAnalysis`). `build_report()` orchestrates extractors that shell out to mediainfo (tags/technical), sox `stat` (acoustic measurements), and a single ffmpeg graph (`astats`, `ebur128`, `drmeter` split from one decode). Phase correlation, clipping counts, silence mapping and the fallback noise floor are **byproducts of the engine's own decode** rather than extra ffmpeg filter passes, so they cost no additional subprocess. The `SpectralEngine` class decodes audio to a numpy float array via ffmpeg pipe and runs an FFT-based **scoring system**: it accumulates a `lossy_score` and a `natural_score` from independent heuristics (HF cutoff, cliff sharpness, banding, side-channel anomaly, noise floor above cutoff, entropy, DSD detection), nets them, and maps the net to a verdict label. The scipy-backed advanced suite adds a **MDCT quantization-error detector** (Derrien, JAES 2019; `_mdct_quant_error`): it re-applies AAC's MDCT + scalefactor quantization to the decoded signal and counts scalefactor bands whose rounding error collapses to near-zero — the fingerprint of an AAC encoder's quantizer surviving in "lossless" PCM. It is the backstop for high-bitrate AAC transcodes that keep full bandwidth and so leave **no lowpass wall** for the cutoff/void/fingerprint rules to catch. Only meaningful at 44.1/48 kHz (the scalefactor-band table is rate-specific); returns `-1` (n/a) otherwise. Two further structural detectors sit alongside it: `_vorbis_grid` (persistent near-zero MDCT coefficients on Vorbis long-block alignments) and `inspect_mqa` (the embedded 36-bit MQA sync word, read from the decoded PCM rather than the tags). Bit-depth authenticity is a **two-prong** check (`check_bit_depth_authenticity`): trailing-zero used-bits analysis plus a noise-floor/effective-dynamic-range prong, so dithered upscales are no longer reported as "verified" hi-res. `make_telegraph_content` in `bot.py` and `print_report`/`print_batch_summary` in `af2.py` are two separate renderers over the same `ForensicReport`.
- **`/log` (`bot.py` + `logchecker.py`)** — EAC/XLD rip log checker. Replies to a `.log` document, POSTs it to `https://logcheck.nirzak.win/api` (multipart `logfile` field), renders all returned database IDs/statuses, then uses the MusicBrainz Disc ID and TOC to find possible physical releases (label, catalog number, barcode, date/area, format, and packaging). MusicBrainz calls are serialized to its one-request-per-second limit and failures degrade to the base log result. A CD TOC can belong to multiple pressings, so matches are deliberately presented as candidates. Intentionally not queued — no heavy processing.
- **`convert.py`** — interactive transcode wizard. Inline-keyboard callback data is positional and colon-delimited: `cv:{chat_id}:{msg_id}:{format}:{mode}:{grade}` (see the module docstring). State keyed in `_convert_sessions`. `_build_ffmpeg_args` maps (format, mode, grade, samplerate) → ffmpeg flags; `_transfer_tags` copies metadata across via mutagen.
- **`cue_split.py`** — CUE-sheet album splitter implemented as a **per-user state machine** in `CUE_WAITING_LIST` (`user_id → state`). Flow: `/cue` on an audio reply kicks off a background download immediately, then the bot prompts for a `.cue` file and optional cover art via follow-up document uploads. The `cue_interceptor` handler in `bot.py` (`filters.document | filters.photo`) catches those uploads and feeds them to `check_and_process_cue_upload`, which advances the state machine and eventually returns the split job.

### Auth gate

`_check_auth(message)` runs at the top of every privileged command. `ADMIN_IDS` bypass all checks; everyone else must be in an allowed chat. Authorization does not restrict topics. Telegram reply routing still preserves thread context:

- **`client.send_*(chat_id=..., message_thread_id=thread_id, ...)`** — must pass `message_thread_id` explicitly, no implicit context.
- **`message.reply_*(...)` / `message.reply_audio(...)` etc.** — must NOT pass `message_thread_id`. Pyrogram derives the thread from the reply anchor automatically, and the kwarg is not accepted by these methods (raises `TypeError` at runtime).

## Conventions worth matching

- **Telegram replies use HTML parse mode** (set globally on the `Client`), not Markdown. Use `<b>`, `<code>`, `<blockquote>`, etc. `/log` is the exception: `logchecker.py` publishes its structured result through the Bot API's `sendRichMessage`, because Pyrofork has no binding for it. It must keep `format_logcheck_result` as a regular-HTML fallback and reply/topic routing must be passed explicitly to the Bot API.
- **Rich-message scope:** `/log` uses headings, compact tables, and collapsible `<details>` blocks so database IDs and alternate pressings don't dominate the report. Do not use rich media blocks for locally generated spectrograms: Bot API rich media requires HTTP(S) URLs.
- Wrap user-facing edits/deletes in `safe_edit` / `safe_delete` (utils) — they swallow `FloodWait` and stale-message errors.
- All temp files live under `/tmp` (e.g. `/tmp/downloads/`); jobs clean up downloaded media and generated spectrograms in a `finally` block. Startup failures are written to the process logs.
- Document-format validation is intentionally strict `.endswith(ext)` checks because Telegram strips extensions from native audio buffers — don't loosen these without understanding the supergroup/document-upload edge cases the git log documents.
- Throttle: `progress_callback` only edits the status message every 3s to avoid FloodWait.
