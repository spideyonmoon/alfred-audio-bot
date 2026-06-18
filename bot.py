#!/usr/bin/env python3
"""
Alfred — Audio Forensics Telegram Interface
"""

import asyncio
import html
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

from af2 import build_report, build_info_report, ForensicReport, generate_spectrogram, compare_reports
from utils import progress_callback, safe_edit, safe_delete
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
COMPARE_TIMEOUT_SEC = 30

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
    parse_mode=ParseMode.HTML,
    # Auto-sleep through FloodWaits up to this many seconds instead of raising.
    # 15s covers the ~11s SendMedia waits seen in practice; longer waits surface
    # as exceptions that safe_edit / upload handlers catch, instead of blocking everything.
    sleep_threshold=15,
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
_compare_sessions: dict[tuple[int, int, int], dict] = {}

# Live status boards — one per (chat_id, thread_id)
# { (chat_id, thread_id): {"msg_id": int, "last_text": str} }
_boards: dict[tuple[int, int], dict] = {}

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

# Executive-summary banner helpers (mirror af2's terminal _print_banner).
_VERDICT_BANNER = {
    "GENUINE":        ("✓", "GENUINE"),
    "LIKELY_GENUINE": ("✓", "LIKELY GENUINE"),
    "CAUTION":        ("~", "CAUTION"),
    "SUSPICIOUS":     ("⚠", "SUSPICIOUS"),
    "LIKELY_LOSSY":   ("✗", "LIKELY LOSSY"),
}

def _channel_label(ch: str) -> str:
    ch = (ch or "").strip()
    return {"1": "Mono", "2": "Stereo"}.get(ch, f"{ch} ch" if ch else "")

def verdict_oneliner(report: ForensicReport) -> str:
    """Single scannable verdict line, e.g. '✗ LIKELY LOSSY · 95/100'. Empty when
    the spectral engine produced no usable verdict."""
    sp = report.authenticity.spectral
    if not sp or sp.verdict_label in ("", "INCONCLUSIVE"):
        return ""
    glyph, label = _VERDICT_BANNER.get(sp.verdict_label, ("·", sp.verdict_label.replace("_", " ")))
    return f"{glyph} {label} · {sp.main_score}/100"

def _compare_session_key(message: Message) -> tuple[int, int, int]:
    return (message.chat.id, message.message_thread_id or 0, message.from_user.id)

def _compare_buttons(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel", callback_data=f"cmp:cancel:{session_id}"),
        InlineKeyboardButton("Complete", callback_data=f"cmp:complete:{session_id}"),
    ]])

def _audio_file_obj(message: Message):
    return message.audio or message.voice or message.document

def _is_supported_audio_message(message: Message) -> bool:
    file_obj = _audio_file_obj(message)
    if not file_obj:
        return False
    if message.document:
        filename = (getattr(message.document, "file_name", "") or "").lower()
        valid_exts = (".flac", ".alac", ".wav", ".aiff", ".aif", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".dsf", ".dff")
        return not filename or filename.endswith(valid_exts)
    return True

def _message_link(message: Message) -> str:
    if getattr(message, "link", None):
        return message.link
    username = getattr(message.chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    chat_id = str(message.chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.id}"
    return ""

def _linked_filename(filename: str, source_msg: Optional[Message]) -> str:
    label = html.escape(filename)
    if not source_msg:
        return f"<b>{label}</b>"
    url = _message_link(source_msg)
    if not url:
        return f"<b>{label}</b>"
    return f'<a href="{html.escape(url, quote=True)}"><b>{label}</b></a>'

def _format_compare_result(reports: list[ForensicReport], sources: Optional[dict[Path, Message]] = None) -> str:
    ordered = compare_reports(reports)
    if not ordered:
        return "❌ <b>No reports were generated.</b>"

    lines = [f"<b>Variant Comparison</b> · <code>{len(ordered)} files</code>", ""]
    for idx, report in enumerate(ordered, 1):
        sp = report.authenticity.spectral
        winner = "★ " if idx == 1 else ""
        filename = _linked_filename(report.filepath.name, (sources or {}).get(report.filepath))
        try:
            sr = f"{int(str(report.technical.sample_rate).strip()) / 1000:g}k"
        except (ValueError, TypeError):
            sr = "?"
        depth = report.technical.precision.replace("-bit", "").strip() or "?"
        cutoff = f"{sp.cutoff_hz / 1000:.1f}k" if sp and sp.cutoff_hz > 0 else "--"
        score = str(sp.main_score) if sp and sp.verdict_label != "INCONCLUSIVE" else "--"
        verdict = html.escape(sp.verdict_label.replace("_", " ") if sp else "INCONCLUSIVE")
        codec = html.escape(sp.codec_fingerprint if sp and sp.codec_fingerprint else "—")
        lines.append(
            f"{idx}. {winner}{filename}\n"
            f"   <code>{depth}/{sr}</code> · cutoff <code>{cutoff}</code> · DR <code>{report.dr_score}</code> · Main <code>{score}</code>\n"
            f"   {verdict} · codec <code>{codec}</code>"
        )

    best = ordered[0]
    bsp = best.authenticity.spectral
    best_tag = f"Main {bsp.main_score} · {bsp.verdict_label.replace('_', ' ')}" if bsp and bsp.verdict_label != "INCONCLUSIVE" else "INCONCLUSIVE"
    lines.extend([
        "",
        f"★ <b>Most authentic:</b> {_linked_filename(best.filepath.name, (sources or {}).get(best.filepath))} ({html.escape(best_tag)})",
        "<i>Ranking assumes these are variants of the same track.</i>",
    ])
    return "\n".join(lines)

async def _expire_compare_session(key: tuple[int, int, int], session_id: str) -> None:
    await asyncio.sleep(COMPARE_TIMEOUT_SEC)
    session = _compare_sessions.get(key)
    if not session or session["id"] != session_id:
        return
    _compare_sessions.pop(key, None)
    prompt = session.get("prompt")
    if prompt:
        try:
            await prompt.edit_text("⌛ <b>Compare cancelled.</b> No completion received within 30 seconds.", parse_mode=ParseMode.HTML)
        except Exception:
            pass

def _clear_compare_session(key: tuple[int, int, int]) -> Optional[dict]:
    session = _compare_sessions.pop(key, None)
    if session:
        task = session.get("timer_task")
        if task:
            task.cancel()
    return session

def _khz_label(sr: str) -> str:
    try:
        return f"{float(sr) / 1000:.1f} kHz"
    except (ValueError, TypeError):
        return ""

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

    # ── 0. EXECUTIVE SUMMARY BANNER ──
    # The conclusion lands first: verdict, confidence bar, main score, headline
    # finding, key specs, loudness, and the lossy/natural signal tally.
    if include_assessment and sp and sp.verdict_label not in ("", "INCONCLUSIVE"):
        glyph, vlabel = _VERDICT_BANNER.get(sp.verdict_label, ("·", sp.verdict_label.replace("_", " ")))
        filled = max(0, min(10, int(sp.net_confidence_pct / 10)))
        conf_bar = "█" * filled + "░" * (10 - filled)

        nodes.append(tag("h3", f"{glyph} VERDICT: {vlabel}"))
        if sp.primary_verdict:
            nodes.append(tag("blockquote", sp.primary_verdict))

        banner = []
        banner += [b("Main Score: "), f"{sp.main_score}/100  {conf_bar}  ({sp.net_confidence_pct:.0f}% confidence)", br()]
        banner += [i("0 = pristine lossless · 100 = certain transcode"), br()]

        specs = " · ".join(s for s in (
            tec.sample_encoding, _khz_label(tec.sample_rate), _channel_label(tec.channels),
            tec.duration, f"{report.file_size_mb:.1f} MB" if report.file_size_mb else "",
        ) if s)
        if specs:
            banner += [b("File: "), specs, br()]

        loud = []
        if report.dr_score and report.dr_score != "N/A": loud.append(report.dr_score)
        if lp.lufs_integrated: loud.append(f"{lp.lufs_integrated} LUFS")
        if lp.true_peak_dbtp:  loud.append(f"{lp.true_peak_dbtp} dBTP")
        elif lp.peak_db:       loud.append(f"peak {lp.peak_db} dBFS")
        if loud:
            banner += [b("Loudness: "), " · ".join(loud), br()]

        banner += [b("Signals: "), f"⚠ {len(sp.evidence)} lossy · ✓ {len(sp.natural_evidence)} natural"]
        nodes.append(tag("p", *banner))
        nodes.append({"tag": "hr", "children": []})

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
    
    if tec.sample_encoding and "flac" in tec.sample_encoding.lower() and report.file_size_mb > 0:
        try:
            # Calculate uncompressed PCM size: SampleRate * Channels * (BitDepth/8) * Duration(s)
            s_rate = float(tec.sample_rate)
            chans = float(tec.channels)
            b_depth = float(tec.precision.replace("-bit", ""))
            
            # Parse duration MM:SS
            d_parts = tec.duration.split(":")
            if len(d_parts) == 2:
                dur_sec = int(d_parts[0]) * 60 + float(d_parts[1])
            elif len(d_parts) == 3:
                dur_sec = int(d_parts[0]) * 3600 + int(d_parts[1]) * 60 + float(d_parts[2])
            else:
                dur_sec = 0
                
            if dur_sec > 0:
                uncomp_bytes = s_rate * chans * (b_depth / 8.0) * dur_sec
                uncomp_mb = uncomp_bytes / (1024 * 1024)
                ratio = (report.file_size_mb / uncomp_mb) * 100
                forensic_lines.extend(add_line("Compression: ", f"{ratio:.1f}", "%"))
        except Exception:
            pass

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
            bd_text += " [⚠ Note: container padding detected via trailing-zero analysis.]"
        verdict_lines.extend(add_line("Bit-Depth Auth: ",   bd_text))

        if auth.phase_correlation and auth.phase_correlation != "N/A":
            verdict_lines.extend(add_line("Phase Correlation: ",
                                          f"{auth.phase_correlation} [{auth.phase_verdict}]"))
        verdict_lines.extend(add_line("Side Channel: ",     auth.side_channel_analysis))
        verdict_lines.extend(add_line("Clipping: ",         auth.clipping_verdict))
        verdict_lines.extend(add_line("Silence: ",          auth.silence_total_pct))
        verdict_lines.extend(add_line("Header Integrity: ", auth.header_integrity))
        if auth.encoder_trace:
            verdict_lines.extend(add_line("Encoder Trace: ", auth.encoder_trace))
        if auth.cassette_rip_detected or auth.vinyl_rip_detected:
            sources = []
            if auth.cassette_rip_detected: sources.append("cassette tape")
            if auth.vinyl_rip_detected:    sources.append("vinyl")
            verdict_lines.extend(add_line("Analog Source: ", " + ".join(sources) + " signature detected"))

        if auth.rg_stored:
            verdict_lines.extend(add_line("RG Tag (stored): ", auth.rg_stored))
            verdict_lines.extend(add_line("RG Measured: ",     auth.rg_measured_lufs))
            verdict_lines.extend(add_line("RG Verdict: ",      auth.rg_verdict))

        if sp and sp.verdict_label != "INCONCLUSIVE":
            verdict_lines.extend([br(), b("── Spectral Engine Verdict [Numpy FFT] ──"), br()])
            verdict_lines.extend(add_line("Conclusion: ", sp.primary_verdict))
            verdict_lines.extend(add_line("Main Score: ",
                f"{sp.main_score}/100  (0 = pristine · 100 = certain transcode)"))
            verdict_lines.extend(add_line("Base Engine: ",
                f"Lossy {sp.lossy_score} − Natural {sp.natural_score} = Net {sp.net_score}/{sp.max_score}"))
            if sp.dsd_detected:
                verdict_lines.extend(add_line("Ultrasonic Noise: ", "⚠ DSD/SACD Transcode Profile detected"))
            verdict_lines.extend(add_line("HF Cutoff: ",         sp.cutoff_hz_str))
            verdict_lines.extend(add_line("Cutoff Variance: ",   f"{sp.cutoff_variance:.1f} Hz²  {sp.cutoff_variance_interp}".strip()))
            verdict_lines.extend(add_line("Cliff Sharpness: ",   f"{sp.cutoff_sharpness_db:.1f} dB/bin  {sp.cutoff_sharpness_interp}".strip()))
            verdict_lines.extend(add_line("HF Energy Ratio: ",   f"{sp.hf_energy_ratio:.5f}  {sp.hf_energy_interp}".strip()))
            verdict_lines.extend(add_line("Side Anomaly: ",      f"{sp.side_anomaly_score:.3f}  {sp.side_interp}".strip()))
            verdict_lines.extend(add_line("Banding Score: ",     f"{sp.banding_score:.3f}  {sp.banding_interp}".strip()))
            verdict_lines.extend(add_line("NF Above Cutoff: ",   f"{sp.nf_above_cutoff_db:.1f} dB  {sp.nf_interp}".strip()))
            verdict_lines.extend(add_line("Low-Pass Filter: ",   ("⚠ Detected — " + sp.lpf_cutoff_str) if sp.lpf_detected else "✓ None detected"))
            verdict_lines.extend(add_line("Spectral Entropy: ",  f"{sp.entropy:.3f}  {sp.entropy_interp}".strip()))

            if sp.scipy_available:
                verdict_lines.extend([br(), b("── Advanced DSP Forensics [scipy] ──"), br()])
                fp_text = f"⚠ {sp.codec_fingerprint}" if sp.codec_fingerprint else "✓ no known encoder wall match"
                verdict_lines.extend(add_line("Codec Fingerprint: ", fp_text))
                res_text = f"⚠ {sp.resample_detected}" if sp.resample_detected else "✓ no foreign-Nyquist artifacts"
                verdict_lines.extend(add_line("Resample Check: ", res_text))
                if sp.segment_walled >= 0:
                    verdict_lines.extend(add_line("Segment Vote: ",
                        f"{sp.segment_walled}/{sp.segment_total} clips walled ≤{sp.segment_wall_hz / 1000:.1f} kHz"))
                if sp.auc_avg_bound_freq > 0:
                    verdict_lines.extend(add_line("auCDtect Bound: ",
                        f"{sp.auc_avg_bound_freq:,.0f} Hz avg · {sp.auc_prob_bound_freq:,.0f} Hz mode  {sp.auc_bound_interp}".strip()))
                if sp.auc_phase_entropy > 0:
                    verdict_lines.extend(add_line("HF Phase Entropy: ",
                        f"{sp.auc_phase_entropy:.2f} bits  {sp.auc_phase_interp}".strip()))
                # MDCT quantization-error lattice (Derrien, JAES 2019) — the backstop
                # for full-bandwidth high-bitrate AAC transcodes that leave no lowpass wall.
                if sp.mdct_quant_score >= 0:
                    verdict_lines.extend(add_line("MDCT Quant. Lattice: ",
                        f"{sp.mdct_quant_score:.3f}  {sp.mdct_quant_interp}".strip()))
                verdict_lines.extend(add_line("Spectral Sparsity: ",
                    f"{sp.spectral_sparsity:.3f}  {sp.sparsity_interp}".strip()))
                if sp.hf_envelope_correlation != 0.0:
                    verdict_lines.extend(add_line("Ultrasonic Corr.: ",
                        f"{sp.hf_envelope_correlation:+.2f}  {sp.hf_env_corr_interp}".strip()))
                if sp.preecho_pct > 0:
                    verdict_lines.extend(add_line("Pre-Echo: ",
                        f"{sp.preecho_pct:.1f}% of transients  [MDCT block smearing]"))
                if sp.aliasing_corr > 0:
                    verdict_lines.extend(add_line("HF Aliasing Corr.: ",
                        f"{sp.aliasing_corr:.2f}  [codec filterbank mirroring]"))
                if sp.mp3_noise_pattern_detected:
                    verdict_lines.extend(add_line("MP3 Subband Comb: ", "⚠ 689 Hz periodic structure detected"))
                if sp.silence_ratio >= 0:
                    verdict_lines.extend(add_line("Silence Dither: ", f"{sp.silence_ratio:.3f}"))
                if sp.vinyl_noise_detected:
                    verdict_lines.extend(add_line("Vinyl Source: ",
                        f"✓ surface noise detected ({sp.vinyl_clicks_per_min:.0f} clicks/min)"))
                if sp.cassette_score >= 30:
                    verdict_lines.extend(add_line("Cassette Source: ",
                        f"✓ tape profile matched (score {sp.cassette_score}/80)"))
                if sp.segment_map:
                    verdict_lines.extend([br(), b("Partially Transcoded Regions:"), br()])
                    for seg_line in sp.segment_map[:6]:
                        verdict_lines.extend([f"  → {seg_line}", br()])

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
# Live Status Board
# ---------------------------------------------------------------------------
def _build_progress_bar(percent: float, length: int = 15) -> str:
    filled = int(round((percent / 100.0) * length))
    empty  = length - filled
    return "⬤" * filled + "○" * empty

def _render_board(chat_id: int, thread_id: int) -> str:
    jobs = [j for j in _active_jobs.values() if j.get("chat_id") == chat_id]
    if not jobs:
        uptime = int(time.monotonic() - _start_time)
        uh, ur = divmod(uptime, 3600); um, us = divmod(ur, 60)
        up_str = f"{uh}h {um}m" if uh else f"{um}m {us}s"
        return (
            f"<b>Alfred</b> · idle\n"
            f"<code>Up {up_str} · {_total_analyses} analyses run</code>"
        )

    blocks = []
    for j in sorted(jobs, key=lambda x: x.get("start_time") or 0):
        pct    = j.get("progress", 0.0)
        bar    = _build_progress_bar(pct)
        status = j.get("status", "—")
        past   = int(time.time() - j["start_time"]) if j.get("start_time") else 0
        pm, ps = divmod(past, 60)
        elapsed = f"{pm}m {ps}s" if pm else f"{ps}s"

        speed = j.get("speed", "")
        eta   = j.get("eta", "")
        pace  = ""
        if speed and speed not in ("0 B/s", ""):
            pace = f" · {speed}"
            if eta and eta not in ("-", ""):
                pace += f" · ETA {eta}"

        name = j.get("username", "?")
        jid  = j["job_id"]
        jtype = j.get("type", "?")

        if past:
            foot = f"╰ {elapsed} · /c_{jid}"
        else:
            foot = f"╰ /c_{jid}"

        blocks.append(
            f"╭ <b>#{jtype}</b> <code>{jid}</code> · @{name}\n"
            f"┊ [{bar}] {pct:.0f}%\n"
            f"┊ {status}{pace}\n"
            f"{foot}"
        )

    return "\n\n".join(blocks)

def _set_job_status(job_id: str, status: str) -> None:
    """Write a job's live status string into _active_jobs so the shared board
    renders it. Replaces per-job status messages — the board is the single view."""
    if job_id and job_id in _active_jobs:
        _active_jobs[job_id]["status"] = status

async def _board_loop():
    """Background task: re-renders and edits all live boards every 3 s."""
    while True:
        await asyncio.sleep(3)
        for key, board in list(_boards.items()):
            chat_id, _ = key
            text = _render_board(chat_id, _)
            if text == board.get("last_text"):
                continue
            try:
                await app.edit_message_text(chat_id, board["msg_id"], text, parse_mode=ParseMode.HTML)
                board["last_text"] = text
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Universal Queue Dispatcher
# ---------------------------------------------------------------------------
async def enqueue_universal_task(job: dict, ctx):
    user_id = job["user_id"]
    if _user_queue_counts[user_id] >= MAX_QUEUE_PER_USER:
        msg = f"⏳ You have reached the maximum queue limit ({MAX_QUEUE_PER_USER}). Please wait for a slot."
        if isinstance(ctx, CallbackQuery):
            await safe_edit(ctx.message, msg)
        else:
            await ctx.reply(msg, quote=True)
        return

    _user_queue_counts[user_id] += 1

    job_id = uuid.uuid4().hex[:6]
    job["job_id"] = job_id

    # Resolve chat context from either a Message or a CallbackQuery
    if isinstance(ctx, CallbackQuery):
        chat_id   = ctx.message.chat.id
        thread_id = ctx.message.message_thread_id or 0
    else:
        chat_id   = ctx.chat.id
        thread_id = ctx.message_thread_id or 0

    username = getattr(ctx.from_user, "username", None) or getattr(ctx.from_user, "first_name", "Unknown")
    _active_jobs[job_id] = {
        "job_id":    job_id,
        "user_id":   user_id,
        "username":  username,
        "chat_id":   chat_id,
        "thread_id": thread_id,
        "type":      job.get("type", "unknown").upper(),
        "filename":  job.get("filename", "Unknown Audio"),
        "status":    "Queued",
        "progress":  0.0,
        "speed":     "0 B/s",
        "eta":       "-",
        "downloaded":"0 B",
        "total":     "0 B",
        "start_time": 0.0,
        "async_task": None,
    }

    # Create or refresh the live board for this (chat, thread)
    key        = (chat_id, thread_id)
    board_text = _render_board(chat_id, thread_id)
    board_reuse_msg = job.pop("board_message", None)

    async def _make_board_msg():
        if board_reuse_msg is not None:
            await board_reuse_msg.edit_text(board_text, parse_mode=ParseMode.HTML)
            return board_reuse_msg
        if isinstance(ctx, CallbackQuery):
            return await app.send_message(
                chat_id, board_text,
                message_thread_id=thread_id or None,
                parse_mode=ParseMode.HTML,
            )
        return await ctx.reply(board_text, parse_mode=ParseMode.HTML, quote=True)

    if key in _boards:
        try:
            await app.edit_message_text(chat_id, _boards[key]["msg_id"], board_text, parse_mode=ParseMode.HTML)
            _boards[key]["last_text"] = board_text
        except Exception:
            board_msg = await _make_board_msg()
            _boards[key] = {"msg_id": board_msg.id, "last_text": board_text}
    else:
        board_msg = await _make_board_msg()
        _boards[key] = {"msg_id": board_msg.id, "last_text": board_text}

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

        # Bail if the job was cancelled while sitting in queue
        if job_id not in _active_jobs or _active_jobs[job_id]["status"] == "Cancelled":
            _user_queue_counts[job["user_id"]] -= 1
            if _user_queue_counts[job["user_id"]] <= 0:
                del _user_queue_counts[job["user_id"]]
            _task_queue.task_done()
            continue

        _active_jobs[job_id]["status"]     = "Preparing..."
        _active_jobs[job_id]["start_time"] = time.time()

        run_task  = None
        job_type  = job.get("type")
        # per-job status message (cnv only); fs reuses the shared board via _active_jobs
        per_job_msg = None

        try:
            if job_type == "fs":
                # No per-job status message: the live board (rendered from
                # _active_jobs) is the single status view for forensic jobs,
                # halving the per-chat edit traffic and FloodWait risk.
                job["payload"]["status_msg"] = None
                job["payload"]["job_id"]     = job_id
                run_task = asyncio.create_task(_run_forensic_job(job["payload"]))

            elif job_type == "cnv":
                import convert
                source_msg  = job["session"]["source_msg"]
                per_job_msg = await source_msg.reply("⚙️ <b>Preparing conversion...</b>", parse_mode=ParseMode.HTML, quote=True)
                job["status_msg"] = per_job_msg
                run_task = asyncio.create_task(convert._run_convert_job(job))

            elif job_type == "cue":
                import cue_split
                run_task = asyncio.create_task(cue_split._run_cue_job(job))

            elif job_type == "cmp":
                job["status_msg"] = None
                run_task = asyncio.create_task(_run_compare_job(job))

        except Exception:
            logger.exception("Queue worker: failed to create per-job status message")

        if run_task:
            _active_jobs[job_id]["async_task"] = run_task
            try:
                await run_task
                _total_analyses += 1
            except asyncio.CancelledError:
                logger.info(f"Task {job_id} natively aborted.")
                sm = per_job_msg or job.get("status_msg")
                if sm:
                    try:
                        await sm.edit_text(f"🛑 <b>Task cancelled.</b> <code>{job_id}</code>", parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
            except Exception:
                logger.exception("Queue worker: unhandled error in job")

        # Tear down the job entry and optionally remove the board if the chat is now idle
        job_chat_id   = _active_jobs.get(job_id, {}).get("chat_id")
        job_thread_id = _active_jobs.get(job_id, {}).get("thread_id", 0)

        user_id = job["user_id"]
        _user_queue_counts[user_id] -= 1
        if _user_queue_counts[user_id] <= 0:
            del _user_queue_counts[user_id]

        _active_jobs.pop(job_id, None)
        _task_queue.task_done()

        # If nothing else is running or queued in this chat, remove the board
        if job_chat_id:
            remaining = [j for j in _active_jobs.values() if j.get("chat_id") == job_chat_id]
            if not remaining:
                key = (job_chat_id, job_thread_id)
                if key in _boards:
                    try:
                        await app.delete_messages(job_chat_id, _boards[key]["msg_id"])
                    except Exception:
                        pass
                    del _boards[key]

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

    job_id = job.get("job_id")

    file_size_mb = getattr(file_obj, "file_size", 0) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await message.reply(f"❌ File exceeds the MTProto limit of <b>{MAX_FILE_SIZE_MB} MB</b>.")
        return

    # No per-job status message: status flows through _active_jobs[job_id] and is
    # rendered by the shared live board. progress_callback also writes there, so
    # passing status_msg=None makes its edit a no-op (only the board telemetry updates).
    status_msg = job.get("status_msg")  # None for fs jobs
    _set_job_status(job_id, "📥 Downloading...")

    file_path_str = None
    spec_path     = None

    try:
        start_time = time.time()
        file_path_str = await client.download_media(
            message=replied,
            file_name="/tmp/downloads/",
            progress=progress_callback,
            progress_args=(status_msg, "Downloading Audio", start_time, [0.0], job_id)
        )
        if not file_path_str:
            raise ValueError("Download yielded an empty path.")

        temp_path = Path(file_path_str)
        filename  = getattr(file_obj, "file_name", temp_path.name)
        logger.info("Analysis start | user=%s chat=%d file=%s flags=%s", username, chat_id, filename, flags)

        if want_spec and not want_info:
            _set_job_status(job_id, "📊 Generating spectrogram...")
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
            else:
                await message.reply("❌ Spectrogram generation failed.", quote=True)
            return

        _set_job_status(job_id, "🔬 Analysing...")
        report = await asyncio.wait_for(
            asyncio.to_thread(build_report, temp_path),
            timeout=300
        )
        spec_path = report.spectrogram_path

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
        
        comp_ratio = ""
        if "FLAC" in codec_raw and hasattr(report, "flac_ratio"):
             comp_ratio = f" | {report.flac_ratio:.1%}"

        page_url = None
        if want_info:
            _set_job_status(job_id, "🌐 Uploading to Telegraph...")
            content  = make_telegraph_content(report, include_assessment=want_assessment)
            title_fmt = f"Analysis on {filename}"
            page_url  = await upload_to_telegraph(title_fmt, content)

        verdict_line = verdict_oneliner(report) if want_assessment else ""
        caption_text = (
            f"<blockquote><b>{artist} - {track_title}</b>\n"
            f"{album}{year}\n"
            f"{tec.duration} | {report.file_size_mb:.1f} MB\n"
            f"{codec_raw} | {sample_rate_fmt}{precision_fmt} | {channels_fmt} | {tec.bit_rate}</blockquote>\n"
        )
        # Verdict one-liner sits at the bottom, just above the Full Analysis link.
        if verdict_line:
            caption_text += f"\n<b>{verdict_line}</b>"
        if want_info and page_url:
            caption_text += f'\n<a href="{page_url}">▸ Full Analysis</a>'
        elif want_info and not page_url:
            caption_text += "\n⚠ Telegraph upload failed."

        # Single result message: spectrogram document (if requested) carries the
        # caption, otherwise a plain reply. The board is torn down by the worker.
        if want_spec and spec_path and spec_path.exists():
            _set_job_status(job_id, "📤 Uploading spectrogram...")
            await message.reply_document(
                document=str(spec_path),
                file_name=f"{Path(filename).stem}_spectrogram.png",
                caption=caption_text,
                progress=progress_callback,
                progress_args=(None, "Uploading Spectrogram", time.time(), [0.0], job_id)
            )
        else:
            await message.reply(caption_text, quote=True, disable_web_page_preview=False)

    except asyncio.TimeoutError:
        await message.reply("❌ <b>Analysis timed out.</b> The file may be too long or the system is overloaded.", quote=True)
    except Exception as e:
        logger.exception("Analysis error")
        await message.reply(f"❌ <b>Process Interrupted:</b> {e}", quote=True)
    finally:
        if file_path_str and Path(file_path_str).exists():
            Path(file_path_str).unlink(missing_ok=True)
        if spec_path and spec_path.exists():
            spec_path.unlink(missing_ok=True)

async def _run_compare_job(job: dict):
    """Download collected audio files and rank variants using the forensic engine."""
    client = job["client"]
    messages: list[Message] = job["messages"]
    origin_message: Message = job["origin_message"]
    job_id = job.get("job_id")

    downloaded: list[Path] = []
    try:
        reports: list[ForensicReport] = []
        sources: dict[Path, Message] = {}
        total = len(messages)
        for idx, media_msg in enumerate(messages, 1):
            file_obj = _audio_file_obj(media_msg)
            filename = getattr(file_obj, "file_name", f"audio_{idx}") or f"audio_{idx}"
            _set_job_status(job_id, f"📥 Downloading {idx}/{total}...")

            file_size_mb = getattr(file_obj, "file_size", 0) / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                raise ValueError(f"{filename} exceeds the {MAX_FILE_SIZE_MB} MB limit")

            file_path_str = await client.download_media(
                message=media_msg,
                file_name="/tmp/downloads/",
                progress=progress_callback,
                progress_args=(None, f"Downloading {idx}/{total}", time.time(), [0.0], job_id),
            )
            if not file_path_str:
                raise ValueError(f"Download failed for {filename}")
            path = Path(file_path_str)
            downloaded.append(path)

            _set_job_status(job_id, f"🔬 Analysing {idx}/{total}...")
            report = await asyncio.wait_for(asyncio.to_thread(build_report, path), timeout=300)
            reports.append(report)
            sources[report.filepath] = media_msg

        _set_job_status(job_id, "🧮 Comparing...")
        result_text = _format_compare_result(reports, sources)
        await origin_message.reply(result_text, parse_mode=ParseMode.HTML, quote=True)

    except asyncio.TimeoutError:
        await origin_message.reply("❌ <b>Comparison timed out.</b> One of the files may be too long.", parse_mode=ParseMode.HTML, quote=True)
    except Exception as e:
        logger.exception("Compare error")
        await origin_message.reply(f"❌ <b>Compare failed:</b> {html.escape(str(e))}", parse_mode=ParseMode.HTML, quote=True)
    finally:
        for path in downloaded:
            if path.exists():
                path.unlink(missing_ok=True)

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
        "<b>Comparison</b>\n"
        "  <code>/compare</code> — Collect variants for 30 seconds, then rank the most authentic\n\n"
        "<b>CUE Splitting</b>\n"
        "  <code>/cue</code> — Reply to an audio file to split via CUE sheet\n\n"
        "<b>Audio Conversion</b>\n"
        "  <code>/cnv &lt;format&gt;</code> — Convert audio\n"
        "  <i>Reply to an audio file. Omit format for interactive menu.</i>\n"
        "  Supported: <code>flac alac mp3 aac ogg opus wav aiff</code>\n\n"
        "<b>Log Checker</b>\n"
        "  <code>/log</code> — Check an EAC/XLD rip log for integrity\n\n"
        "<b>Utility</b>\n"
        "  <code>/stats</code> — Queue status and bot statistics\n"
        "  <code>/help</code> — This message\n"
    )
    await message.reply(text)

# ---------------------------------------------------------------------------
# /log — EAC/XLD rip log checker
# ---------------------------------------------------------------------------
_LOGCHECK_API = "https://logcheck.nirzak.win/api"

_CHECKSUM_LABELS = {
    "checksum_ok":       "✅ OK",
    "checksum_missing":  "⚠ Missing",
    "checksum_mismatch": "❌ Mismatch",
}

def _score_emoji(score: int) -> str:
    if score == 100: return "💯"
    if score >= 80:  return "🟢"
    if score >= 50:  return "🟡"
    return "🔴"

@app.on_message(filters.command("log"))
async def logcheck_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return

    replied = message.reply_to_message
    if not replied or not replied.document:
        await message.reply("↩️ <i>Reply to a <code>.log</code> file with <code>/log</code>.</i>")
        return

    filename = getattr(replied.document, "file_name", "") or ""
    if not filename.lower().endswith(".log"):
        await message.reply("❌ Only <code>.log</code> files are supported.")
        return

    status_msg = await message.reply("📥 <b>Downloading log...</b>", parse_mode=ParseMode.HTML, quote=True)
    file_path = None
    try:
        file_path = await client.download_media(replied, file_name="/tmp/downloads/")
        if not file_path:
            await safe_edit(status_msg, "❌ Download failed.", parse_mode=ParseMode.HTML)
            return

        await safe_edit(status_msg, "🔍 <b>Checking log...</b>", parse_mode=ParseMode.HTML)

        async with httpx.AsyncClient(timeout=30.0) as http:
            with open(file_path, "rb") as f:
                resp = await http.post(_LOGCHECK_API, files={"logfile": (filename, f)})

        if resp.status_code != 200:
            await safe_edit(status_msg, f"❌ API error: <code>{resp.status_code}</code>", parse_mode=ParseMode.HTML)
            return

        data = resp.json()
        score    = data.get("score", "?")
        ripper   = data.get("ripper", "Unknown")
        version  = data.get("ripper_version", "")
        checksum = _CHECKSUM_LABELS.get(data.get("checksum_state", ""), data.get("checksum_state", ""))
        details  = data.get("details", [])
        emoji    = _score_emoji(score) if isinstance(score, int) else "❓"

        ripper_line = f"{ripper} {version}".strip()
        details_text = "\n".join(f"  • {d}" for d in details) if details else "  <i>None</i>"

        text = (
            f"<blockquote><b>{filename}</b></blockquote>\n\n"
            f"{emoji} <b>Score:</b> <code>{score}/100</code>\n"
            f"🎙 <b>Ripper:</b> <code>{ripper_line}</code>\n"
            f"🔐 <b>Checksum:</b> {checksum}\n\n"
            f"<b>Details:</b>\n{details_text}"
        )
        await safe_edit(status_msg, text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.exception("Log check error")
        await safe_edit(status_msg, f"❌ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)
    finally:
        if file_path and Path(file_path).exists():
            Path(file_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# /pong - Diagnostic
# ---------------------------------------------------------------------------
@app.on_message(filters.command("pong"))
async def pong_command(client: Client, message: Message):
    await message.reply("🏓 PING!")

# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------
@app.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    chat_id   = message.chat.id
    thread_id = message.message_thread_id or 0
    key       = (chat_id, thread_id)
    text      = _render_board(chat_id, thread_id)

    if key in _boards:
        # Relocate: resend first so there's no blank gap, then delete the old one
        new_msg = await message.reply(text, parse_mode=ParseMode.HTML, quote=True)
        old_id  = _boards[key]["msg_id"]
        _boards[key] = {"msg_id": new_msg.id, "last_text": text}
        try:
            await app.delete_messages(chat_id, old_id)
        except Exception:
            pass
    else:
        new_msg = await message.reply(text, parse_mode=ParseMode.HTML, quote=True)
        _boards[key] = {"msg_id": new_msg.id, "last_text": text}

@app.on_message(filters.regex(r"^/(?:c|cancel)_([a-f0-9]{6})(?:@\S+)?$"))
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

@app.on_message(filters.command("compare"))
async def compare_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return

    key = _compare_session_key(message)
    existing = _clear_compare_session(key)
    if existing and existing.get("prompt"):
        try:
            await existing["prompt"].edit_text("🛑 <b>Previous compare session replaced.</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    session_id = uuid.uuid4().hex[:8]
    prompt = await message.reply(
        "Send the files you want to compare, then click <b>Complete</b> when done.",
        parse_mode=ParseMode.HTML,
        quote=True,
        reply_markup=_compare_buttons(session_id),
    )
    _compare_sessions[key] = {
        "id": session_id,
        "origin_message": message,
        "prompt": prompt,
        "messages": [],
        "timer_task": asyncio.create_task(_expire_compare_session(key, session_id)),
    }

@app.on_callback_query(filters.regex(r"^cmp:(cancel|complete):([a-f0-9]{8})$"))
async def compare_callback(client: Client, query: CallbackQuery):
    action = query.matches[0].group(1)
    session_id = query.matches[0].group(2)
    msg = query.message
    key = (msg.chat.id, msg.message_thread_id or 0, query.from_user.id)
    session = _compare_sessions.get(key)

    if not session or session["id"] != session_id:
        await query.answer("This compare session is no longer active.", show_alert=True)
        return

    if action == "cancel":
        _clear_compare_session(key)
        await query.answer("Cancelled.")
        await msg.edit_text("🛑 <b>Compare cancelled.</b>", parse_mode=ParseMode.HTML)
        return

    messages = list(session.get("messages", []))
    if len(messages) < 2:
        await query.answer("Send at least two audio files first.", show_alert=True)
        return

    _clear_compare_session(key)
    await query.answer("Comparison queued.")

    names = []
    for media_msg in messages:
        file_obj = _audio_file_obj(media_msg)
        names.append(getattr(file_obj, "file_name", "audio") or "audio")

    job = {
        "type": "cmp",
        "user_id": query.from_user.id,
        "filename": f"{len(messages)} variants",
        "origin_message": session["origin_message"],
        "board_message": msg,
        "messages": messages,
        "captured_names": names,
    }
    await enqueue_universal_task(job, query)

@app.on_message(filters.command(["forensic", "fs"]))
async def forensic_command(client: Client, message: Message):
    if not _check_auth(message):
        await _reject_auth(message)
        return

    user_id = message.from_user.id
    replied = message.reply_to_message
    if not replied:
        await message.reply("↩️ <i>Reply to an audio file with <code>/fs</code> [flag].</i>")
        return

    file_obj = replied.audio or replied.voice or replied.document
    if not file_obj:
        await message.reply("❌ The replied message does not contain an audio file.")
        return

    filename = getattr(file_obj, "file_name", "Unknown Audio") or "Unknown Audio"
    filename = filename.lower()

    if replied.document:
        valid_exts = (".flac", ".alac", ".wav", ".aiff", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".dsf", ".dff")
        if filename != "unknown audio" and not filename.endswith(valid_exts):
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

@app.on_message(filters.audio | filters.voice | filters.document | filters.photo)
async def cue_interceptor(client: Client, message: Message):
    """Intercept uploads for active compare and CUE state machines."""
    if message.from_user:
        key = _compare_session_key(message)
        session = _compare_sessions.get(key)
        if session:
            if _is_supported_audio_message(message):
                session["messages"].append(message)
                count = len(session["messages"])
                prompt = session.get("prompt")
                if prompt:
                    try:
                        await prompt.edit_text(
                            f"Send the files you want to compare, then click <b>Complete</b> when done.\n\n"
                            f"Captured: <code>{count}</code>",
                            parse_mode=ParseMode.HTML,
                            reply_markup=_compare_buttons(session["id"]),
                        )
                    except Exception:
                        pass
            return

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

        workers    = [asyncio.create_task(_queue_worker()) for _ in range(MAX_CONCURRENT_JOBS)]
        board_task = asyncio.create_task(_board_loop())
        try:
            await idle()
        finally:
            for w in workers:
                w.cancel()
            board_task.cancel()

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
