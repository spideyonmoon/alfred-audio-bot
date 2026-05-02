"""
convert.py — Interactive Audio Conversion Module (Pyrogram-native)

Callback data schema:
  cv:{source_chat_id}:{source_msg_id}:{format}           → after format chosen
  cv:{source_chat_id}:{source_msg_id}:{format}:{mode}    → after VBR/CBR/ABR or level
  cv:{source_chat_id}:{source_msg_id}:{format}:{mode}:{grade} → final, start conversion

Formats:
  mp3, flac, alac, aac, ogg, opus, wav, aiff
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.enums import ParseMode

from utils import progress_callback, run_async_subprocess

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, TDRC, TCON
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.wave import WAVE
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Formats that are lossy (input). Used for ⚠ disclaimer on lossy→lossless.
# ---------------------------------------------------------------------------
_LOSSY_FORMATS = {".mp3", ".aac", ".ogg", ".opus", ".wma"}
_LOSSLESS_FORMATS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".alac", ".w64"}

# ---------------------------------------------------------------------------
# Session state: keyed by (chat_id, source_msg_id)
# ---------------------------------------------------------------------------
_convert_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------
async def handle_convert_command(client: Client, message: Message):
    replied = message.reply_to_message
    if not replied:
        await message.reply(
            "↩️ <i>Reply to an audio file with <code>/cnv [format]</code>.</i>",
            parse_mode=ParseMode.HTML
        )
        return

    file_obj = replied.audio or replied.voice or replied.document
    if not file_obj:
        await message.reply("❌ The replied message doesn't contain an audio file.")
        return

    if replied.document:
        filename = getattr(file_obj, "file_name", "").lower()
        valid_exts = (".flac", ".alac", ".wav", ".aiff", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".dsf", ".dff")
        if filename and not filename.endswith(valid_exts):
            await message.reply("❌ Invalid format. Audio conversion can only process audio files.")
            return

    args        = message.command[1:]
    target_fmt  = args[0].lower().strip() if args else ""

    # Build session key
    sess_key = f"{replied.chat.id}:{replied.id}"
    _convert_sessions[sess_key] = {
        "source_msg":   replied,
        "file_obj":     file_obj,
        "chat_id":      message.chat.id,
        "thread_id":    message.message_thread_id,
        "user_id":      message.from_user.id,
    }

    if target_fmt in ("mp3", "flac", "alac", "aac", "ogg", "opus", "wav", "aiff"):
        return await _show_mode_menu(client, message, sess_key, target_fmt)
    else:
        # No format given → show format picker
        await _show_format_menu(client, message, sess_key)
        return None


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
async def _show_format_menu(client: Client, message: Message, sess_key: str):
    chat_id, src_id = sess_key.split(":")
    rows = [
        [
            InlineKeyboardButton("FLAC",  callback_data=f"cv:{chat_id}:{src_id}:flac"),
            InlineKeyboardButton("ALAC",  callback_data=f"cv:{chat_id}:{src_id}:alac"),
        ],
        [
            InlineKeyboardButton("WAV",   callback_data=f"cv:{chat_id}:{src_id}:wav"),
            InlineKeyboardButton("AIFF",  callback_data=f"cv:{chat_id}:{src_id}:aiff"),
        ],
        [
            InlineKeyboardButton("MP3",   callback_data=f"cv:{chat_id}:{src_id}:mp3"),
            InlineKeyboardButton("AAC",   callback_data=f"cv:{chat_id}:{src_id}:aac"),
        ],
        [
            InlineKeyboardButton("OGG",   callback_data=f"cv:{chat_id}:{src_id}:ogg"),
            InlineKeyboardButton("Opus",  callback_data=f"cv:{chat_id}:{src_id}:opus"),
        ],
    ]
    await message.reply(
        "🎵 <b>Convert to what format?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
        quote=True
    )


async def _show_mode_menu(client: Client, ctx, sess_key: str, fmt: str):
    """Shows VBR/CBR/ABR menu for MP3; level for FLAC/ALAC; bitrate for AAC/OGG/Opus;
    or goes straight to conversion for WAV/AIFF."""
    chat_id, src_id = sess_key.split(":")

    def btn(label, mode):
        return InlineKeyboardButton(label, callback_data=f"cv:{chat_id}:{src_id}:{fmt}:{mode}")

    if fmt == "mp3":
        rows = [
            [btn("VBR", "vbr"), btn("CBR", "cbr")],
            [btn("ABR", "abr")]
        ]
        text = "🎵 <b>MP3 encoding mode?</b>"

    elif fmt == "flac":
        rows = [
            [btn("Level 0 (fastest)", "0")],
            [btn("Level 5 (default)", "5")],
            [btn("Level 8 (smallest)", "8")],
            [btn("Custom (Bit/Sample Rate)", "custom")]
        ]
        text = f"🎵 <b>{fmt.upper()} compression level?</b>"

    elif fmt == "aac":
        rows = [
            [btn("128k", "128"), btn("192k", "192")],
            [btn("256k", "256"), btn("320k", "320")]
        ]
        text = "🎵 <b>AAC bitrate?</b>"

    elif fmt == "ogg":
        rows = [
            [btn("Q3 (~112k)", "3"), btn("Q5 (~160k)", "5")],
            [btn("Q7 (~224k)", "7"), btn("Q9 (~320k)", "9")]
        ]
        text = "🎵 <b>OGG Vorbis quality?</b>"

    elif fmt == "opus":
        rows = [
            [btn("64k", "64"), btn("96k", "96")],
            [btn("128k", "128"), btn("192k", "192")],
            [btn("256k", "256")]
        ]
        text = "🎵 <b>Opus bitrate?</b>"

    else:
        # WAV, AIFF, ALAC — no sub-menu needed
        session = _convert_sessions.pop(sess_key, None)
        if not session: return None
        return {
            "type": "cnv",
            "user_id": ctx.from_user.id,
            "filename": getattr(session["file_obj"], "file_name", "unknown"),
            "fmt": fmt,
            "mode": "pcm",
            "grade": "default",
            "ctx": ctx,
            "session": session
        }

    text_fn = getattr(ctx, "reply", None) or getattr(ctx, "edit_message_text", None)

    if isinstance(ctx, Message):
        await ctx.reply(text, parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(rows), quote=True)
    else:
        # CallbackQuery — edit the existing message
        await ctx.edit_message_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(rows))
    return None


async def _show_grade_menu(client: Client, query: CallbackQuery,
                            sess_key: str, fmt: str, mode: str):
    """Shows grade selection (V0-V4 for VBR; bitrate for CBR/ABR)."""
    chat_id, src_id = sess_key.split(":")

    def btn(label, grade):
        return InlineKeyboardButton(label, callback_data=f"cv:{chat_id}:{src_id}:{fmt}:{mode}:{grade}")

    if mode == "vbr":
        rows = [
            [btn("V0 (best)", "0"), btn("V1", "1")],
            [btn("V2", "2"), btn("V3", "3")],
            [btn("V4", "4")]
        ]
        text = "🎵 <b>MP3 VBR quality?</b>\n<i>V0 ≈ 245 kbps avg — highest quality</i>"
    elif fmt == "flac" and mode == "custom":
        rows = [
            [btn("16-bit", "16"), btn("24-bit", "24")]
        ]
        text = "🎵 <b>FLAC Bit Depth?</b>"
    else:
        # CBR / ABR
        rows = [
            [btn("128k", "128"), btn("192k", "192")],
            [btn("256k", "256"), btn("320k", "320")]
        ]
        text = f"🎵 <b>MP3 {mode.upper()} bitrate?</b>"

    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup(rows))
    return None


async def _show_samplerate_menu(client: Client, query: CallbackQuery,
                             sess_key: str, fmt: str, mode: str, grade: str):
    """Shows sample rate selection for FLAC custom mode."""
    chat_id, src_id = sess_key.split(":")

    def btn(label, sr): return InlineKeyboardButton(label, callback_data=f"cv:{chat_id}:{src_id}:{fmt}:{mode}:{grade}:{sr}")

    rows = [
        [btn("44.1 kHz", "44100"), btn("48 kHz", "48000")],
        [btn("96 kHz", "96000"), btn("192 kHz", "192000")]
    ]
    text = f"🎵 <b>FLAC Sample Rate?</b>"

    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup(rows))
    return None

# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------
async def handle_convert_callback(client: Client, query: CallbackQuery):
    await query.answer()
    parts = query.data.split(":")
    # cv:{chat_id}:{src_id}:{fmt}[:{mode}[:{grade}[:{samplerate}]]]
    if len(parts) < 4:
        return

    _, chat_id, src_id, fmt = parts[0], parts[1], parts[2], parts[3]
    mode       = parts[4] if len(parts) >= 5 else None
    grade      = parts[5] if len(parts) >= 6 else None
    samplerate = parts[6] if len(parts) >= 7 else None

    sess_key = f"{chat_id}:{src_id}"
    if sess_key not in _convert_sessions:
        await query.answer("Session expired. Please run /cnv again.", show_alert=True)
        return

    user_id = query.from_user.id
    if _convert_sessions[sess_key]["user_id"] != user_id:
        await query.answer("This menu isn't for you.", show_alert=True)
        return

    # Route by depth
    if mode is None:
        # Format chosen → show mode/level menu
        return await _show_mode_menu(client, query, sess_key, fmt)

    elif grade is None and (fmt == "mp3" and mode in ("vbr", "cbr", "abr") or (fmt == "flac" and mode == "custom")):
        # MP3 mode chosen or FLAC custom chosen → show grade/bitdepth menu
        return await _show_grade_menu(client, query, sess_key, fmt, mode)

    elif samplerate is None and fmt == "flac" and mode == "custom":
        # FLAC bit depth chosen → show sample rate menu
        return await _show_samplerate_menu(client, query, sess_key, fmt, mode, grade)

    else:
        # All params collected → return complete job payload for global scheduling
        effective_mode  = mode  or "default"
        effective_grade = grade or "default"
        session = _convert_sessions.pop(sess_key, None)
        if not session:
            await query.answer("Session expired. Please run /cnv again.", show_alert=True)
            return None
        return {
            "type": "cnv",
            "user_id": user_id,
            "filename": getattr(session["file_obj"], "file_name", "unknown"),
            "fmt": fmt,
            "mode": effective_mode,
            "grade": effective_grade,
            "samplerate": samplerate or "default",
            "ctx": query,
            "session": session
        }


# ---------------------------------------------------------------------------
# Conversion executor
# ---------------------------------------------------------------------------
_LOSSY_IN_LOSSLESS_WARNING = (
    "\n\n⚠ <i>Note: the source is a lossy format. Re-encoding to lossless preserves "
    "the file but does <b>not</b> recover lost audio data.</i>"
)

async def _run_convert_job(job: dict):
    client    = job["client"]
    ctx       = job["ctx"]
    session   = job["session"]
    fmt       = job["fmt"]
    mode      = job["mode"]
    grade     = job["grade"]
    samplerate = job.get("samplerate", "default")

    source_msg = session["source_msg"]
    file_obj   = session["file_obj"]
    chat_id    = session["chat_id"]
    thread_id  = session["thread_id"]
    status_msg = job["status_msg"]
    
    if not status_msg:
        logger.error("Job provided no status_msg fallback framework!")
        return

    await status_msg.edit_text("📥 <b>Downloading source file...</b>", parse_mode=ParseMode.HTML)

    tmp_dir     = tempfile.mkdtemp(prefix="alfred_cv_")
    src_path    = None
    out_path    = None

    try:
        import time
        start_time = time.time()
        # Download source (passing a directory path ending with a slash tells Pyrogram it's a directory)
        src_path = await client.download_media(
            message=source_msg, 
            file_name=tmp_dir + "/",
            progress=progress_callback,
            progress_args=(status_msg, "Downloading Source", start_time, [0.0], job.get("job_id"))
        )
        if not src_path or not os.path.exists(src_path):
            await status_msg.edit_text("❌ Download failed.")
            return

        src_suffix   = Path(src_path).suffix.lower()
        src_stem     = Path(src_path).stem
        src_is_lossy = src_suffix in _LOSSY_FORMATS
        tgt_is_lossless = fmt in ("flac", "alac", "wav", "aiff")

        # Build FFmpeg command. Protect against identical filesystem string overwritings by 
        # injecting the output directly into a dedicated `/out/` child directory.
        out_ext, cmd_suffix, out_label = _build_ffmpeg_args(fmt, mode, grade, samplerate)
        out_filename = f"{src_stem}.{out_ext}"
        
        out_dir = os.path.join(tmp_dir, "out")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, out_filename)

        ffmpeg_cmd = ["ffmpeg", "-y", "-i", src_path, "-vn"] + cmd_suffix + [out_path]

        await status_msg.edit_text(
            f"⚙️ <b>Converting to {out_label}...</b>",
            parse_mode=ParseMode.HTML
        )

        retcode, stderr = await run_async_subprocess(ffmpeg_cmd)
        if retcode != 0 or not os.path.exists(out_path):
            error_trace = f"\n<pre>{stderr[-500:]}</pre>" if stderr else ""
            await status_msg.edit_text(f"❌ Conversion failed. FFmpeg returned an error:{error_trace}", parse_mode=ParseMode.HTML)
            return

        # Transfer tags from source to output
        await asyncio.to_thread(_transfer_tags, src_path, out_path, fmt)

        # Build caption
        orig_name    = getattr(file_obj, "file_name", Path(src_path).name)
        out_size_mb  = os.path.getsize(out_path) / (1024 * 1024)
        src_size_mb  = os.path.getsize(src_path) / (1024 * 1024)

        caption = (
            f"✅ <b>Conversion complete</b>\n"
            f"<blockquote>"
            f"From: <code>{Path(src_path).suffix.lstrip('.').upper()}</code> ({src_size_mb:.1f} MB)\n"
            f"To: <code>{out_label}</code> ({out_size_mb:.1f} MB)\n"
            f"Source: {orig_name}"
            f"</blockquote>"
        )

        if src_is_lossy and tgt_is_lossless:
            caption += _LOSSY_IN_LOSSLESS_WARNING

        await status_msg.edit_text("📤 <b>Uploading...</b>", parse_mode=ParseMode.HTML)

        await client.send_document(
            chat_id             = chat_id,
            document            = out_path,
            file_name           = out_filename,
            caption             = caption,
            parse_mode          = ParseMode.HTML,
            message_thread_id   = thread_id,
            reply_to_message_id = source_msg.id,
            progress            = progress_callback,
            progress_args       = (status_msg, "Uploading Converted File", time.time(), [0.0], job.get("job_id"))
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Conversion error")
        try:
            await status_msg.edit_text(f"❌ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)





def _build_ffmpeg_args(fmt: str, mode: str, grade: str, samplerate: str = "default") -> tuple[str, list, str]:
    """Returns (output_extension, [extra ffmpeg args], display_label)."""

    if fmt == "mp3":
        if mode == "vbr":
            g = grade if grade.isdigit() else "0"
            return "mp3", ["-c:a", "libmp3lame", "-q:a", g], f"MP3 VBR V{g}"
        elif mode == "cbr":
            bps = grade if grade.isdigit() else "320"
            return "mp3", ["-c:a", "libmp3lame", "-b:a", f"{bps}k"], f"MP3 CBR {bps}k"
        else:  # abr
            bps = grade if grade.isdigit() else "320"
            return "mp3", ["-c:a", "libmp3lame", "-b:a", f"{bps}k", "-abr", "1"], f"MP3 ABR {bps}k"

    elif fmt == "flac":
        if mode == "custom":
            depth = grade if grade in ("16", "24") else "16"
            sr_str = samplerate if samplerate in ("44100", "48000", "96000", "192000") else "44100"
            sfmt = "s16" if depth == "16" else "s32"
            
            args = ["-c:a", "flac", "-compression_level", "5", "-sample_fmt", sfmt, "-ar", sr_str]
            return "flac", args, f"FLAC {depth}-bit {int(sr_str)//1000}kHz"
        else:
            lvl = grade if grade.isdigit() else "5"
            return "flac", ["-c:a", "flac", "-compression_level", lvl], f"FLAC Level {lvl}"

    elif fmt == "alac":
        return "m4a", ["-c:a", "alac"], f"ALAC Target"

    elif fmt == "aac":
        bps = grade if grade.isdigit() else "256"
        return "m4a", ["-c:a", "aac", "-b:a", f"{bps}k"], f"AAC {bps}k"

    elif fmt == "ogg":
        q = grade if grade.isdigit() else "5"
        return "ogg", ["-c:a", "libvorbis", "-q:a", q], f"OGG Vorbis Q{q}"

    elif fmt == "opus":
        bps = grade if grade.isdigit() else "192"
        return "ogg", ["-c:a", "libopus", "-b:a", f"{bps}k"], f"Opus {bps}k"

    elif fmt == "wav":
        return "wav", ["-c:a", "pcm_s16le"], "WAV PCM 16-bit"

    elif fmt == "aiff":
        return "aiff", ["-c:a", "pcm_s16be"], "AIFF PCM 16-bit"

    # Fallback passthrough
    return fmt, ["-c", "copy"], fmt.upper()


# ---------------------------------------------------------------------------
# Tag transfer (source → output)
# ---------------------------------------------------------------------------
def _transfer_tags(src_path: str, dst_path: str, dst_fmt: str) -> None:
    """Copy basic tags and embedded cover art from src to dst using mutagen."""
    if not HAS_MUTAGEN:
        return

    try:
        src = MutagenFile(src_path, easy=True)
        if src is None:
            return

        # Extract common fields
        def g(key):
            v = src.get(key, [])
            return str(v[0]) if v else ""

        tags = {
            "title":       g("title"),
            "artist":      g("artist"),
            "album":       g("album"),
            "date":        g("date"),
            "genre":       g("genre"),
            "tracknumber": g("tracknumber"),
        }

        # Extract cover art from source
        cover_data: Optional[bytes] = None
        cover_mime = "image/jpeg"

        src_raw = MutagenFile(src_path)
        if src_raw:
            # FLAC
            if hasattr(src_raw, "pictures") and src_raw.pictures:
                p = src_raw.pictures[0]
                cover_data = p.data
                cover_mime = p.mime
            # MP3/ID3
            elif hasattr(src_raw, "tags") and src_raw.tags:
                for key in src_raw.tags.keys():
                    if key.startswith("APIC"):
                        cover_data = src_raw.tags[key].data
                        cover_mime = src_raw.tags[key].mime
                        break
            # MP4
            elif hasattr(src_raw, "tags") and src_raw.tags and "covr" in src_raw.tags:
                cover_data = bytes(src_raw.tags["covr"][0])
                cover_mime = "image/jpeg"

    except Exception as e:
        logger.warning("Could not read source tags: %r", e)
        return

    try:
        ext = Path(dst_path).suffix.lower()

        if ext == ".flac":
            dst = FLAC(dst_path)
            for k, v in tags.items():
                if v: dst[k] = v
            if cover_data:
                p = Picture()
                p.data = cover_data
                p.type = 3
                p.mime = cover_mime
                dst.add_picture(p)
            dst.save()

        elif ext == ".mp3":
            try: dst = MP3(dst_path, ID3=ID3)
            except Exception: dst = MP3(dst_path); dst.add_tags()
            if dst.tags is None: dst.add_tags()
            if tags["title"]:       dst.tags.add(TIT2(encoding=3, text=tags["title"]))
            if tags["artist"]:      dst.tags.add(TPE1(encoding=3, text=tags["artist"]))
            if tags["album"]:       dst.tags.add(TALB(encoding=3, text=tags["album"]))
            if tags["date"]:        dst.tags.add(TDRC(encoding=3, text=tags["date"]))
            if tags["genre"]:       dst.tags.add(TCON(encoding=3, text=tags["genre"]))
            if tags["tracknumber"]: dst.tags.add(TRCK(encoding=3, text=tags["tracknumber"]))
            if cover_data:
                dst.tags.add(APIC(encoding=3, mime=cover_mime, type=3,
                                  desc="Cover", data=cover_data))
            dst.save()

        elif ext in (".m4a", ".mp4"):
            dst = MP4(dst_path)
            if dst.tags is None: dst.add_tags()
            if tags["title"]:  dst.tags["\xa9nam"] = [tags["title"]]
            if tags["artist"]: dst.tags["\xa9ART"] = [tags["artist"]]
            if tags["album"]:  dst.tags["\xa9alb"] = [tags["album"]]
            if tags["date"]:   dst.tags["\xa9day"] = [tags["date"]]
            if tags["genre"]:  dst.tags["\xa9gen"] = [tags["genre"]]
            try:
                tn = tags["tracknumber"]
                if "/" in tn:
                    n, t = tn.split("/", 1)
                    dst.tags["trkn"] = [(int(n), int(t))]
                elif tn:
                    dst.tags["trkn"] = [(int(tn), 0)]
            except Exception: pass
            if cover_data:
                fmt_flag = MP4Cover.FORMAT_PNG if cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
                dst.tags["covr"] = [MP4Cover(cover_data, imageformat=fmt_flag)]
            dst.save()

        elif ext in (".wav",):
            try: dst = WAVE(dst_path)
            except Exception: return
            if dst.tags is None: dst.add_tags()
            if tags["title"]:  dst.tags.add(TIT2(encoding=3, text=tags["title"]))
            if tags["artist"]: dst.tags.add(TPE1(encoding=3, text=tags["artist"]))
            if tags["album"]:  dst.tags.add(TALB(encoding=3, text=tags["album"]))
            if tags["date"]:   dst.tags.add(TDRC(encoding=3, text=tags["date"]))
            if cover_data:
                dst.tags.add(APIC(encoding=3, mime=cover_mime, type=3,
                                  desc="Cover", data=cover_data))
            dst.save()

        # AIFF: mutagen support is limited — skip tagging

    except Exception as e:
        logger.warning("Tag transfer failed for %s: %r", dst_path, e)
