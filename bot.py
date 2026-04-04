#!/usr/bin/env python3
"""
Alfred — Audio Forensics Telegram Interface
"""

import asyncio
import json
import logging
import os
import re
import time
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

from dotenv import load_dotenv
import httpx

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from af2 import build_report, build_info_report, ForensicReport, generate_spectrogram
from utils import progress_callback
import health
import cue_split
import convert

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
API_ID      = os.getenv("API_ID")
API_HASH    = os.getenv("API_HASH")

ALLOWED_CHATS: dict[int, set] = {}
try:
    raw_chats = json.loads(os.getenv("ALLOWED_CHATS", "{}"))
    ALLOWED_CHATS = {int(cid): set(topics) for cid, topics in raw_chats.items()}
except json.JSONDecodeError:
    pass

ALLOWED_TOPICS: set = set(json.loads(os.getenv("ALLOWED_TOPICS", "[]")))
ADMIN_IDS: set[int] = set(json.loads(os.getenv("ADMIN_IDS", "[]")))
MAX_FILE_SIZE_MB = 1500

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# On HuggingFace, run purely in-memory to safely isolate Auth Keys from local runs
is_hf = bool(os.getenv("SPACE_ID"))

app = Client(
    name=":memory:" if is_hf else "alfred_session",
    in_memory=is_hf,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML
)

# ---------------------------------------------------------------------------
# Stats & Queue
# ---------------------------------------------------------------------------
_start_time     = time.monotonic()
_total_analyses = 0
_task_queue: asyncio.Queue = asyncio.Queue()
_user_queue_counts: defaultdict[int, int] = defaultdict(int) 
MAX_QUEUE_PER_USER = 5
MAX_CONCURRENT_JOBS = 4
_active_jobs: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
def _check_auth(message: Message) -> bool:
    if message.from_user and message.from_user.id in ADMIN_IDS:
        return True

    chat_id  = message.chat.id
    topic_id = message.message_thread_id or 0
    if chat_id not in ALLOWED_CHATS:
        return False
    allowed_topics = ALLOWED_CHATS.get(chat_id, set())
    return topic_id in allowed_topics or topic_id in ALLOWED_TOPICS

async def _reject_auth(message: Message) -> None:
    chat_id  = message.chat.id
    topic_id = message.message_thread_id or 0
    if chat_id not in ALLOWED_CHATS:
        await message.reply("❌ <b>Not authorized.</b> This bot is not enabled for this chat.")
    else:
        await message.reply("❌ <b>Not authorized.</b> This bot is not enabled in this topic.")

# ---------------------------------------------------------------------------
# Telegraph
# ---------------------------------------------------------------------------
async def upload_to_telegraph(title: str, content: str) -> Optional[str]:
    access_token = os.getenv("TELEGRAPH_TOKEN")
    if not access_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "access_token": access_token,
                "title": title[:250],
                "author_name": "Alfred",
                "content": content,
                "return_content": True,
            }
            resp = await client.post("https://api.telegra.ph/createPage", json=payload)
            data = resp.json()
            if data.get("ok"):
                return data["result"]["url"]
            logger.error("Telegraph API rejected payload: %s", data)
            return None
    except Exception as e:
        logger.error("Telegraph upload failed: %r", e)
        return None

# ---------------------------------------------------------------------------
# Telegraph content builder
# ---------------------------------------------------------------------------
def _fmt_stat_key(key: str) -> str:
    return re.sub(r"([A-Z])", r" \1", key).strip().title()

def make_telegraph_content(report: ForensicReport, include_assessment: bool = True) -> str:
    """Compiles the Telegraph DOM. Pass include_assessment=False to skip Alfred's Verdict."""

    def tag(name, *children):
        return {"tag": name, "children": [str(c) if not isinstance(c, dict) else c
                                           for c in children if c is not None and str(c).strip() != ""]}
    def b(text): return tag("b", text)
    def i(text): return tag("em", text)
    def br():    return tag("br")
    def a(href, text): return {"tag": "a", "attrs": {"href": href}, "children": [text]}

    def add_line(label, value, suffix=""):
        v_str = str(value).strip() if value is not None else ""
        if v_str and v_str not in ["N/A", "Unknown", "None", "0.00", "0", "()", "N/A kHz", "UnknownkHz"]:
            return [b(label), f"{v_str}{suffix}", br()]
        return []

    t, tec, lp, auth, sp = report.tags, report.technical, report.loudness, report.authenticity, report.authenticity.spectral
    nodes = []

    nodes.append(tag("p",
        "A brief about the terminologies is available ",
        a("https://telegra.ph/A-Brief-03-24", "here"),
        "."
    ))
    nodes.append({"tag": "hr", "children": []})

    # ── 1. METADATA TAG ──
    nodes.append(tag("h3", "METADATA TAG"))
    meta_lines = []
    meta_lines.extend(add_line("Title: ",        t.title))
    meta_lines.extend(add_line("Artist: ",       t.artist))
    meta_lines.extend(add_line("Album: ",        t.album))
    meta_lines.extend(add_line("Album Artist: ", t.album_artist))
    meta_lines.extend(add_line("Year: ",         t.date))
    meta_lines.extend(add_line("BPM: ",          t.bpm))
    meta_lines.extend(add_line("Comments: ",     t.comments))
    meta_lines.extend(add_line("Rip Quality: ",  t.comment_quality))
    if not meta_lines:
        meta_lines = ["No internal metadata tags found."]
    nodes.append(tag("p", *meta_lines))

    # ── 2. AUDIO FORENSIC ──
    nodes.append(tag("h3", "AUDIO FORENSIC"))
    forensic_lines = []
    forensic_lines.extend(add_line("Encoding: ",    tec.sample_encoding))
    forensic_lines.extend(add_line("Bit Rate: ",    tec.bit_rate))
    forensic_lines.extend(add_line("Sample Rate: ", tec.sample_rate, " Hz"))
    forensic_lines.extend(add_line("Channels: ",    tec.channels))
    forensic_lines.extend(add_line("Precision: ",   tec.precision))
    forensic_lines.extend(add_line("File Size: ",   f"{report.file_size_mb:.1f}", " MB"))
    forensic_lines.extend(add_line("Duration: ",    tec.duration))

    forensic_lines.extend([br(), b("── Level Bookends ──"), br()])
    forensic_lines.extend(add_line("Signal Ceiling: ", lp.peak_db,      " dBFS"))
    forensic_lines.extend(add_line("Noise Floor: ",    lp.noise_floor_db," dBFS"))
    forensic_lines.extend(add_line("RMS Loudness: ",   lp.rms_db,        " dBFS"))
    forensic_lines.extend(add_line("RMS Peak: ",       lp.rms_peak_db,   " dBFS"))
    forensic_lines.extend(add_line("RMS Trough: ",     lp.rms_trough_db, " dBFS"))

    forensic_lines.extend([br(), b("── EBU R128 [FFmpeg ebur128] ──"), br()])
    forensic_lines.extend(add_line("LUFS Integrated: ", lp.lufs_integrated,    " LUFS"))
    forensic_lines.extend(add_line("Loudness Range: ",  lp.lufs_range,          " LU"))
    forensic_lines.extend(add_line("True Peak: ",       lp.true_peak_dbtp,     " dBTP"))
    forensic_lines.extend(add_line("Momentary Max: ",   lp.lufs_momentary_max, " LUFS"))
    forensic_lines.extend(add_line("Short-term Max: ",  lp.lufs_shortterm_max, " LUFS"))

    forensic_lines.extend([br(), b("── Dynamics & Integrity ──"), br()])
    forensic_lines.extend(add_line("DR Score (EBU): ",             report.dr_score))
    forensic_lines.extend(add_line("DR [FFmpeg drmeter]: ",        lp.dynamic_range_db,  " dB"))
    forensic_lines.extend(add_line("Crest Factor: ",               lp.crest_factor_db,   " dB"))
    forensic_lines.extend(add_line("Flat Factor: ",                lp.flat_factor))
    forensic_lines.extend(add_line("SoX Entropy: ",               lp.sox_entropy))
    forensic_lines.extend(add_line("DC Offset: ",                  lp.dc_offset))
    forensic_lines.extend(add_line("Peak Events [FFmpeg astats]: ",lp.peak_count))
    forensic_lines.extend(add_line("Zero Crossing Rate: ",         lp.zero_crossings_rate))

    sox_groups = {
        "Peak Levels": ["maximumAmplitude","minimumAmplitude","meanAmplitude","midlineAmplitude","rmsAmplitude","meanNorm"],
        "Delta":       ["maximumDelta","minimumDelta","meanDelta","rmsDelta"],
        "Samples":     ["samplesRead","lengthSeconds","roughFrequency"],
        "Scaling":     ["scaledBy","volumeAdjustment"],
    }
    forensic_lines.extend([br(), b("── Acoustic Measurements [SoX stat] ──"), br()])
    for gname, keys in sox_groups.items():
        for key in keys:
            if key in report.sox_stats:
                forensic_lines.extend(add_line(f"{_fmt_stat_key(key)}: ", report.sox_stats[key]))
    nodes.append(tag("p", *forensic_lines))

    # ── 3. ALFRED'S VERDICT ── (optional)
    if include_assessment:
        nodes.append(tag("h3", "ALFRED'S VERDICT"))
        nodes.append(tag("p", i(
            "Disclaimer: This assessment utilizes heuristic DSP analysis and is inherently fragile. "
            "Results may be entirely accurate, partially correct, or completely misidentified."
        )))

        verdict_lines = []
        bd_text = auth.bit_depth_authentic
        if bd_text and "padded" in bd_text.lower():
            bd_text += " [⚠ Note: SoX heuristics for container padding are experimental.]"
        verdict_lines.extend(add_line("Bit-Depth Auth [SoX]: ", bd_text))

        if auth.phase_correlation and auth.phase_correlation != "N/A":
            verdict_lines.extend(add_line("Phase Corr [FFmpeg aphasemeter]: ",
                                          f"{auth.phase_correlation} [{auth.phase_verdict}]"))
        verdict_lines.extend(add_line("Clipping [FFmpeg astats]: ",       auth.clipping_verdict))
        verdict_lines.extend(add_line("Silence [FFmpeg silencedetect]: ", auth.silence_total_pct))

        if auth.rg_stored:
            verdict_lines.extend(add_line("RG Tag (stored): ", auth.rg_stored))
            verdict_lines.extend(add_line("RG Measured: ",     auth.rg_measured_lufs))
            verdict_lines.extend(add_line("RG Verdict: ",      auth.rg_verdict))

        if sp and sp.verdict_label != "INCONCLUSIVE":
            verdict_lines.extend([br(), b("── Spectral Engine Verdict [Numpy FFT] ──"), br()])
            verdict_lines.extend(add_line("Conclusion: ", sp.primary_verdict))
            verdict_lines.extend(add_line("Algorithm Score: ",
                f"Lossy {sp.lossy_score} − Natural {sp.natural_score} = Net {sp.net_score}/{sp.max_score}"))
            if getattr(sp, "dsd_detected", False):
                verdict_lines.extend(add_line("Ultrasonic Noise: ", "⚠ DSD/SACD Transcode Profile detected"))
            verdict_lines.extend(add_line("HF Cutoff: ",         sp.cutoff_hz_str))
            verdict_lines.extend(add_line("Cutoff Variance: ",   f"{sp.cutoff_variance:.1f} Hz² {sp.cutoff_variance_interp}".strip()))
            verdict_lines.extend(add_line("Cliff Sharpness: ",   f"{sp.cutoff_sharpness_db:.1f} dB/bin {sp.cutoff_sharpness_interp}".strip()))
            verdict_lines.extend(add_line("HF Energy Ratio: ",   f"{sp.hf_energy_ratio:.5f} {sp.hf_energy_interp}".strip()))
            verdict_lines.extend(add_line("Side Anomaly: ",      f"{sp.side_anomaly_score:.3f} {sp.side_interp}".strip()))
            verdict_lines.extend(add_line("Banding Score: ",     f"{sp.banding_score:.3f} {sp.banding_interp}".strip()))
            verdict_lines.extend(add_line("NF Above Cutoff: ",   f"{sp.nf_above_cutoff_db:.1f} dB {sp.nf_interp}".strip()))
            verdict_lines.extend(add_line("Low-Pass Filter: ",   "Detected" if sp.lpf_detected else "None detected"))
            verdict_lines.extend(add_line("Spectral Entropy: ",  f"{sp.entropy:.3f} {sp.entropy_interp}".strip()))

            nodes.append(tag("p", *verdict_lines))

            if auth.silence_sections:
                cap = 15
                shown = auth.silence_sections[:cap]
                label = f"Silence Sections (Showing {cap} of {len(auth.silence_sections)}):" \
                        if len(auth.silence_sections) > cap else "Silence Sections:"
                nodes.append(tag("p", b(label)))
                nodes.append(tag("ul", *[tag("li", s) for s in shown]))

            if sp.evidence:
                nodes.append(tag("h4", "Lossy Indicators"))
                nodes.append(tag("ul", *[tag("li", e) for e in sp.evidence]))
            if sp.natural_evidence:
                nodes.append(tag("h4", "Natural Indicators"))
                nodes.append(tag("ul", *[tag("li", e) for e in sp.natural_evidence]))
            if sp.caveats:
                nodes.append(tag("h4", "Context Notes"))
                nodes.append(tag("ul", *[tag("li", c) for c in sp.caveats]))
        else:
            if verdict_lines:
                nodes.append(tag("p", *verdict_lines))
            else:
                nodes.append(tag("p", "No authenticity data available."))
            nodes.append(tag("p", "Spectral analysis inconclusive or failed."))

    return json.dumps(nodes, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Universal Queue Dispatcher
# ---------------------------------------------------------------------------
async def enqueue_universal_task(job: dict, ctx):
    user_id = job["user_id"]
    if _user_queue_counts[user_id] >= MAX_QUEUE_PER_USER:
        msg = f"⏳ **You have reached the maximum queue limit ({MAX_QUEUE_PER_USER}). Please wait for a slot.**"
        if isinstance(ctx, CallbackQuery):
            await ctx.edit_message_text(msg)
        else:
            await ctx.reply(msg, quote=True)
        return

    _user_queue_counts[user_id] += 1
    
    job_id = uuid.uuid4().hex[:6]
    job["job_id"] = job_id
    
    username = getattr(ctx.from_user, "username", None) or getattr(ctx.from_user, "first_name", "Unknown")
    _active_jobs[job_id] = {
        "job_id": job_id,
        "user_id": user_id,
        "username": username,
        "type": job.get("type", "unknown").upper(),
        "filename": job.get("filename", "Unknown Audio"),
        "status": "Queued",
        "progress": 0.0,
        "speed": "0 B/s",
        "eta": "-",
        "downloaded": "0 B",
        "total": "0 B",
        "start_time": 0.0,
        "async_task": None
    }
    
    queue_pos = _task_queue.qsize()
    text = f"✅ Queued. Position: <b>{queue_pos + 1}</b>.\n⋗ Stop: /c_{job_id}"
    
    if isinstance(ctx, CallbackQuery):
        status_msg = await ctx.edit_message_text(text, parse_mode=ParseMode.HTML)
    else:
        status_msg = await ctx.reply(text, parse_mode=ParseMode.HTML, quote=True)

    job["status_msg"] = status_msg
    job["client"] = getattr(ctx, "_client", app)
    await _task_queue.put(job)

# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------
async def _queue_worker():
    global _total_analyses
    while True:
        job = await _task_queue.get()
        job_id = job["job_id"]
        
        # Fast exit if cancelled during wait time
        if job_id not in _active_jobs or _active_jobs[job_id]["status"] == "Cancelled":
            _user_queue_counts[job["user_id"]] -= 1
            if _user_queue_counts[job["user_id"]] <= 0:
                del _user_queue_counts[job["user_id"]]
            _task_queue.task_done()
            continue
            
        _active_jobs[job_id]["status"] = "Preparing..."
        _active_jobs[job_id]["start_time"] = time.time()
        
        run_task = None
        job_type = job.get("type")
        
        if job_type == "fs":
            job["payload"]["job_id"] = job_id
            run_task = asyncio.create_task(_run_forensic_job(job["payload"]))
        elif job_type == "cnv":
            import convert
            run_task = asyncio.create_task(convert._run_convert_job(job))
        elif job_type == "cue":
            import cue_split
            run_task = asyncio.create_task(cue_split._run_cue_job(job))
            
        if run_task:
            _active_jobs[job_id]["async_task"] = run_task
            try:
                await run_task
                _total_analyses += 1
            except asyncio.CancelledError:
                logger.info(f"Task {job_id} natively aborted.")
                try:
                    await job["status_msg"].edit_text(f"🛑 <b>Task Cancelled.</b> <code>{job_id}</code>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            except Exception:
                logger.exception("Queue worker: unhandled error in job")
        
        user_id = job["user_id"]
        _user_queue_counts[user_id] -= 1
        if _user_queue_counts[user_id] <= 0:
            del _user_queue_counts[user_id]
        
        _active_jobs.pop(job_id, None)
        _task_queue.task_done()

async def _run_forensic_job(job: dict):
    """Execute a single /fs analysis job from the queue."""
    client          = job["client"]
    message         = job["message"]     # original /fs command message
    replied         = job["replied"]
    file_obj        = job["file_obj"]
    flags           = job["flags"]

    want_spec       = flags["spec"]
    want_info       = flags["info"]
    want_assessment = flags["assessment"]

    chat_id   = message.chat.id
    topic_id  = message.message_thread_id
    user_id   = message.from_user.id
    username  = message.from_user.username or message.from_user.first_name

    file_size_mb = getattr(file_obj, "file_size", 0) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await message.reply(f"❌ File exceeds the MTProto limit of <b>{MAX_FILE_SIZE_MB} MB</b>.")
        return

    status_msg = job.get("status_msg")
    if status_msg:
        await status_msg.edit_text("📥 <b>Downloading...</b>", parse_mode=ParseMode.HTML)
    else:
        status_msg = await message.reply("📥 <b>Downloading...</b>", quote=True)
        
    file_path_str = None
    spec_path     = None

    try:
        start_time = time.time()
        file_path_str = await client.download_media(
            message=replied,
            file_name="/tmp/downloads/",
            progress=progress_callback,
            progress_args=(status_msg, "Downloading Audio", start_time, [0.0], job.get("job_id"))
        )
        if not file_path_str:
            raise ValueError("Download yielded an empty path.")

        temp_path = Path(file_path_str)
        filename  = getattr(file_obj, "file_name", temp_path.name)
        logger.info("Analysis start | user=%s chat=%d file=%s flags=%s", username, chat_id, filename, flags)

        # Spec-only: just generate and send the spectrogram, done
        if want_spec and not want_info:
            await status_msg.edit_text("📊 <b>Generating spectrogram...</b>")
            spec_path = await asyncio.wait_for(
                asyncio.to_thread(generate_spectrogram, temp_path),
                timeout=120
            )
            if spec_path and spec_path.exists():
                await message.reply_document(
                    document=str(spec_path),
                    file_name=f"{Path(filename).stem}_spectrogram.png",
                    caption=f"<b>Spectrogram</b> — {filename}"
                )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Spectrogram generation failed.")
            return

        # Full/partial analysis
        await status_msg.edit_text("🔬 <b>Analysing...</b>")
        report = await asyncio.wait_for(
            asyncio.to_thread(build_report, temp_path),
            timeout=300
        )
        spec_path = report.spectrogram_path

        # Build caption
        t, tec = report.tags, report.technical
        artist      = t.artist   or "Unknown Artist"
        track_title = t.title    or "Unknown Title"
        album       = t.album    or "Unknown Album"
        year        = f" [{t.date}]" if t.date else ""

        codec_raw    = tec.sample_encoding.split()[-1].upper() if tec.sample_encoding else "UNKNOWN"
        ch_raw       = tec.channels.strip()
        channels_fmt = {"1": "Mono", "2": "Stereo", "6": "5.1", "8": "7.1"}.get(ch_raw, ch_raw)
        sr_raw       = tec.sample_rate.strip()
        sample_rate_fmt = f"{int(sr_raw):,} Hz" if sr_raw.isdigit() else sr_raw
        precision_fmt   = f" | {tec.precision}" if tec.precision else ""

        page_url = None
        if want_info:
            await status_msg.edit_text("🌐 <b>Uploading to Telegraph...</b>")
            content  = make_telegraph_content(report, include_assessment=want_assessment)
            title_fmt = f"Analysis on {filename}"
            page_url  = await upload_to_telegraph(title_fmt, content)

        safe_url = page_url or "#"
        caption_text = (
            f"<blockquote><b>{artist} - {track_title}</b>\n"
            f"{album}{year}\n"
            f"{tec.duration} | {report.file_size_mb:.1f} MB\n"
            f"{codec_raw} | {sample_rate_fmt}{precision_fmt} | {channels_fmt} | {tec.bit_rate}</blockquote>\n\n"
        )
        if want_info and page_url:
            caption_text += f'<a href="{page_url}">Full Analysis</a>'
        elif want_info and not page_url:
            caption_text += "⚠ Telegraph upload failed."

        # Send results
        if want_spec and spec_path and spec_path.exists():
            await status_msg.edit_text("📤 <b>Uploading Spectrogram...</b>")
            await message.reply_document(
                document=str(spec_path),
                file_name=f"{Path(filename).stem}_spectrogram.png",
                caption=caption_text if want_info else f"<b>Spectrogram</b> — {filename}",
                progress=progress_callback,
                progress_args=(status_msg, "Uploading Spectrogram", time.time(), [0.0], job.get("job_id"))
            )
            await status_msg.delete()
        elif want_info:
            await status_msg.edit_text(caption_text, disable_web_page_preview=False)
        else:
            await status_msg.delete()

    except asyncio.TimeoutError:
        await status_msg.edit_text("❌ <b>Analysis timed out.</b> The file may be too long or the system is overloaded.")
    except Exception as e:
        logger.exception("Analysis error")
        await status_msg.edit_text(f"❌ <b>Process Interrupted:</b> {e}")
    finally:
        if file_path_str and Path(file_path_str).exists():
            Path(file_path_str).unlink(missing_ok=True)
        if spec_path and spec_path.exists():
            spec_path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply(
        "Greetings. I am <b>Alfred</b>.\n\n"
        "I meticulously analyze audio files, evaluating their authenticity, "
        "spectral integrity, and technical characteristics.\n\n"
        "<i>Send /help for the full command reference.</i>"
    )

# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    text = (
        "<b>Alfred — Command Reference</b>\n\n"
        "<b>Forensics</b>\n"
        "  <code>/fs</code> — Full report (spectrogram + info + assessment)\n"
        "  <code>/fs -spec</code> — Spectrogram only\n"
        "  <code>/fs -info</code> — Text info + assessment (no spectrogram)\n"
        "  <code>/fs -na</code> — Info + spectrogram, no assessment\n"
        "  <code>/fs -nas</code> — Text info only, no assessment, no spectrogram\n\n"
        "<b>CUE Splitting</b>\n"
        "  <code>/cue</code> — Reply to an audio file to split via CUE sheet\n\n"
        "<b>Audio Conversion</b>\n"
        "  <code>/cnv &lt;format&gt;</code> — Convert audio\n"
        "  <i>Reply to an audio file. Omit format for interactive menu.</i>\n"
        "  Supported: <code>flac alac mp3 aac ogg opus wav aiff</code>\n\n"
        "<b>Utility</b>\n"
        "  <code>/stats</code> — Queue status and bot statistics\n"
        "  <code>/help</code> — This message\n"
    )
    await message.reply(text)

# ---------------------------------------------------------------------------
# /pong - Diagnostic
# ---------------------------------------------------------------------------
@app.on_message(filters.command("pong"))
async def pong_command(client: Client, message: Message):
    await message.reply("🏓 PING!")

# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------
def _build_progress_bar(percent: float, length: int = 15) -> str:
    filled = int(round((percent / 100.0) * length))
    empty = length - filled
    return "⬤" * filled + "○" * empty

@app.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    uptime_sec = int(time.monotonic() - _start_time)
    h, rem     = divmod(uptime_sec, 3600)
    m, s       = divmod(rem, 60)
    
    cpu_usage = psutil.cpu_percent() if psutil else 0.0
    mem_usage = psutil.virtual_memory().percent if psutil else 0.0
    disk_usage = psutil.disk_usage('/').free / (1024**3) if psutil else 0.0
    
    out = []
    for jid, st in _active_jobs.items():
        if not st.get("async_task"): continue
        pct = st["progress"]
        bar = _build_progress_bar(pct)
        passed = int(time.time() - st["start_time"])
        pm, ps = divmod(passed, 60)
        ph, pm = divmod(pm, 60)
        past_str = f"{ph}h{pm}m{ps}s" if ph else f"{pm}m{ps}s"
        
        block = (
            f"╭ <b>Task By {st['username']}</b>\n"
            f"┊ [{bar}] {pct:.1f}%\n"
            f"┊ Status   : {st['status']}\n"
            f"┊ Done     : {st['downloaded']}\n"
            f"┊ Total    : {st['total']}\n"
            f"┊ Speed    : {st['speed']}\n"
            f"┊ ETA      : {st['eta']}\n"
            f"┊ Past     : {past_str}\n"
            f"╰ Mode     : #{st['type']}\n"
            f"⋗ Stop : /c_{jid}\n"
        )
        out.append(block)
    
    jobs_str = "\n".join(out) if out else "<i>No active tasks.</i>\n"
    
    sys_text = (
        f"⌬ <b>𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗦</b>\n"
        f"╭ CPU  : {cpu_usage}%\n"
        f"┊ RAM  : {mem_usage}%\n"
        f"┊ FREE : {disk_usage:.2f}GB\n"
        f"╰ UP   : {h}h{m}m{s}s\n"
    )
    await message.reply(f"{jobs_str}\n{sys_text}")

@app.on_message(filters.regex(r"^/c_(.+)$") | filters.regex(r"^/cancel_(.+)$"))
async def cancel_command(client: Client, message: Message):
    job_id = message.matches[0].group(1)
    if job_id not in _active_jobs:
        await message.reply("❌ Task not found or already completed.")
        return
        
    job_state = _active_jobs[job_id]
    if message.from_user.id != job_state["user_id"] and message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ You don't have permission to cancel this task.")
        return
        
    task = job_state.get("async_task")
    if task:
        task.cancel()
        await message.reply(f"🛑 Kill signal transmitted to <code>{job_id}</code>.")
    else:
        job_state["status"] = "Cancelled"
        await message.reply(f"🛑 Task <code>{job_id}</code> permanently removed from queue.")

# ---------------------------------------------------------------------------
# /fs — main forensic command
# ---------------------------------------------------------------------------
def _parse_fs_flags(args: list[str]) -> dict:
    """Parse /fs flags into a dict of booleans."""
    flag = (args[0].lower() if args else "")
    if flag == "-spec":
        return {"spec": True,  "info": False, "assessment": False}
    if flag == "-info":
        return {"spec": False, "info": True,  "assessment": True}
    if flag == "-na":
        return {"spec": True,  "info": True,  "assessment": False}
    if flag == "-nas":
        return {"spec": False, "info": True,  "assessment": False}
    # default: full
    return {"spec": True, "info": True, "assessment": True}

@app.on_message(filters.command(["forensic", "fs"]))
async def forensic_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return

    replied = message.reply_to_message
    if not replied:
        await message.reply("↩️ <i>Reply to an audio file with <code>/fs</code> [flag].</i>")
        return

    file_obj = replied.audio or replied.voice or replied.document
    if not file_obj:
        await message.reply("❌ The replied message does not contain an audio file.")
        return

    if replied.document:
        filename = getattr(file_obj, "file_name", "").lower()
        valid_exts = (".flac", ".alac", ".wav", ".aiff", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".dsf", ".dff")
        if filename and not filename.endswith(valid_exts):
            await message.reply("❌ Invalid format. Audio forensics can only process audio files.")
            return

    args  = message.command[1:]
    flags = _parse_fs_flags(args)

    job = {
        "type": "fs",
        "user_id": user_id,
        "filename": filename,
        "payload": {
            "client":   client,
            "message":  message,
            "replied":  replied,
            "file_obj": file_obj,
            "flags":    flags,
            "user_id":  user_id,
            "username": message.from_user.username or message.from_user.first_name,
            "filename": filename,
        }
    }
    await enqueue_universal_task(job, message)

# ---------------------------------------------------------------------------
# /cue — CUE splitting (Pyrogram-native, wired from cue_split.py)
# ---------------------------------------------------------------------------
@app.on_message(filters.command("cue"))
async def cuesplit_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return
    await cue_split.handle_cuesplit_command(client, message)

@app.on_message(filters.document | filters.photo)
async def cue_interceptor(client: Client, message: Message):
    """Intercept document/photo messages for CUE state machine."""
    job = await cue_split.check_and_process_cue_upload(client, message)
    if isinstance(job, dict):
        await enqueue_universal_task(job, message)

@app.on_callback_query(filters.regex(r"^cuesplit_"))
async def cuesplit_callback(client: Client, query: CallbackQuery):
    job = await cue_split.handle_cuesplit_callback(client, query)
    if isinstance(job, dict):
        await enqueue_universal_task(job, query)

# ---------------------------------------------------------------------------
# /cnv — audio conversion
# ---------------------------------------------------------------------------
@app.on_message(filters.command("cnv"))
async def convert_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return
    job = await convert.handle_convert_command(client, message)
    if isinstance(job, dict):
        await enqueue_universal_task(job, message)

@app.on_callback_query(filters.regex(r"^cv:"))
async def convert_callback(client: Client, query: CallbackQuery):
    job = await convert.handle_convert_callback(client, query)
    if isinstance(job, dict):
        await enqueue_universal_task(job, query)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _on_start():
    """Startup coroutine: called by app.run().
    Since we pass a coroutine to app.run(), Pyrogram doesn't automatically
    start the client. We must use `async with app:` to start/stop the client,
    then spawn the worker and idle."""
    from pyrogram import idle
    
    # On HuggingFace Spaces, SPACE_ID env var is set automatically.
    # Start the minimal health server IMMEDIATELY so HF startup probes pass
    # BEFORE Pyrogram blocks to negotiate the MTProto connection to Telegram.
    if os.getenv("SPACE_ID"):
        asyncio.create_task(health.start_health_server(port=7860))
        logger.info("HuggingFace Space detected — health server started on :7860")

    async with app:
        logger.info("Alfred (MTProto) is now online and standing by.")

        workers = [asyncio.create_task(_queue_worker()) for _ in range(MAX_CONCURRENT_JOBS)]
        try:
            await idle()
        finally:
            for w in workers:
                w.cancel()

if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        logger.error("Missing critical environment variables (BOT_TOKEN, API_ID, or API_HASH).")
    else:
        try:
            app.run(_on_start())
        except Exception as e:
            import traceback
            import health
            with open("/tmp/crash.log", "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
            logger.error("Fatal startup error. Storing traceback to /tmp/crash.log and starting debug health server.")
            async def serve_crash():
                await health.start_health_server(port=7860)
            asyncio.run(serve_crash())# cache breaker 123
