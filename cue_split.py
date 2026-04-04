"""
cue_split.py — CUE Sheet Splitter (Pyrogram-native)

Flow:
  1. User replies to audio with /cue
  2. Audio download starts immediately in background
  3. Bot prompts for .cue file
  4. Bot prompts for album art (or skip)
  5. Splitting begins — awaits download task (likely already done)
  6. Tracks are tagged and uploaded
"""

import asyncio
import logging
import os
import re
import shutil
import glob
import tempfile

from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.enums import ParseMode

from utils import run_async_subprocess

# Mutagen for tagging
try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, TDRC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.wave import WAVE
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    logging.getLogger(__name__).warning("mutagen not installed — CUE tracks will not be tagged.")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
# user_id → state dict
CUE_WAITING_LIST: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------
async def handle_cuesplit_command(client: Client, message: Message):
    """Triggered by /cue. Must be a reply to an audio/document."""
    user_id = message.from_user.id
    if not message.reply_to_message:
        await message.reply("❌ <b>Reply to an audio file</b> with this command.", parse_mode=ParseMode.HTML)
        return

    target = message.reply_to_message
    file_obj = target.audio or target.document or target.voice
    if not file_obj:
        await message.reply("❌ The replied message does not contain an audio file.")
        return

    if target.document:
        filename = getattr(file_obj, "file_name", "").lower()
        valid_exts = (".flac", ".alac", ".wav", ".aiff", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".dsf", ".dff")
        if filename and not filename.endswith(valid_exts):
            await message.reply("❌ Invalid format. Audio splitting can only process audio files.")
            return

    user_id = message.from_user.id
    if user_id in CUE_WAITING_LIST:
        await message.reply("⏳ You already have an active CUE session. Please finish or restart.")
        return

    # Determine filename for the download
    if target.audio:
        audio_filename = target.audio.file_name or "audio.flac"
    else:
        audio_filename = target.document.file_name or "audio.flac"

    work_dir = os.path.join(tempfile.gettempdir(), f"alfred_cs_{user_id}_{target.id}")
    os.makedirs(work_dir, exist_ok=True)
    local_audio_path = os.path.join(work_dir, audio_filename)

    # ── Start download in background IMMEDIATELY ──
    download_task = asyncio.create_task(
        _download_audio(client, target, local_audio_path)
    )

    prompt = await message.reply(
        "✅ <b>Audio queued for download.</b>\n\n"
        "Now send the <b>.cue file</b> as a document.",
        parse_mode=ParseMode.HTML,
        quote=True
    )

    CUE_WAITING_LIST[user_id] = {
        "audio_msg":       target,
        "audio_path":      local_audio_path,
        "download_task":   download_task,
        "status":          "waiting_cue",
        "chat_id":         message.chat.id,
        "thread_id":       message.message_thread_id,
        "prompt_msg_id":   prompt.id,
        "work_dir":        work_dir,
        "cue_path":        None,
        "cover_path":      None,
    }


async def _download_audio(client: Client, msg: Message, dest: str) -> bool:
    """Background task: download audio via MTProto to dest path."""
    try:
        await client.download_media(msg, file_name=dest)
        return os.path.exists(dest) and os.path.getsize(dest) > 0
    except Exception as e:
        logger.error("CUE background download failed: %r", e)
        return False


# ---------------------------------------------------------------------------
# Document / photo interceptor  (called from bot.py for all doc/photo msgs)
# ---------------------------------------------------------------------------
async def check_and_process_cue_upload(client: Client, message: Message) -> bool:
    """Returns True if message was consumed by the CUE state machine."""
    user_id = message.from_user.id if message.from_user else None
    if not user_id or user_id not in CUE_WAITING_LIST:
        return False

    state = CUE_WAITING_LIST[user_id]

    # ── STATE 1: waiting for .cue file ──
    if state["status"] == "waiting_cue":
        doc = message.document
        if not doc or not doc.file_name.lower().endswith(".cue"):
            return False

        work_dir       = state["work_dir"]
        local_cue_path = os.path.join(work_dir, "input.cue")

        # Download CUE in-memory to avoid Pyrogram tiny-file shutil.move bug
        cue_io = await client.download_media(message, in_memory=True)
        if cue_io:
            with open(local_cue_path, "wb") as f:
                f.write(cue_io.getbuffer())

        # Clean up prompts
        try:
            await client.delete_messages(state["chat_id"], [state["prompt_msg_id"]])
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass

        state["cue_path"] = local_cue_path
        state["status"]   = "waiting_art"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Skip Album Art ⏩", callback_data=f"cuesplit_skip_{user_id}")
        ]])
        art_prompt = await client.send_message(
            chat_id              = state["chat_id"],
            message_thread_id    = state["thread_id"],
            text                 = "🎨 <b>CUE received.</b>\n\nSend album art (photo/image doc) or click Skip.",
            parse_mode           = ParseMode.HTML,
            reply_markup         = keyboard,
            reply_to_message_id  = state["audio_msg"].id,
        )
        state["prompt_msg_id"] = art_prompt.id
        return True

    # ── STATE 2: waiting for album art ──
    elif state["status"] == "waiting_art":
        is_photo    = bool(message.photo)
        is_img_doc  = bool(message.document and message.document.mime_type
                           and "image" in message.document.mime_type)
        if not (is_photo or is_img_doc):
            return False

        work_dir       = state["work_dir"]
        local_art_path = os.path.join(work_dir, "cover.jpg")

        art_io = await client.download_media(message, in_memory=True)
        if art_io:
            with open(local_art_path, "wb") as f:
                f.write(art_io.getbuffer())
                
        state["cover_path"]  = local_art_path
        state["art_msg_id"]  = message.id
        state["status"] = "processing"
        
        # Clean up wait status and dispatch the Job payload to bot.py
        del CUE_WAITING_LIST[user_id]
        
        return {
            "type": "cue",
            "user_id": user_id,
            "filename": "CUE Project",
            "state": state,
            "ctx": message
        }

    return False


# ---------------------------------------------------------------------------
# Skip-art callback
# ---------------------------------------------------------------------------
async def handle_cuesplit_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if query.data != f"cuesplit_skip_{user_id}":
        await query.answer("This button isn't for you.", show_alert=True)
        return

    if user_id not in CUE_WAITING_LIST or CUE_WAITING_LIST[user_id]["status"] != "waiting_art":
        await query.answer("Session expired.", show_alert=True)
        return

    await query.answer()
    state = CUE_WAITING_LIST.pop(user_id)
    state["cover_path"] = None
    state["art_msg_id"] = None
    state["status"]     = "processing"

    return {
        "type": "cue",
        "user_id": user_id,
        "filename": "CUE Project",
        "state": state,
        "ctx": query
    }


# ---------------------------------------------------------------------------
# Core splitting pipeline
# ---------------------------------------------------------------------------
async def _run_cue_job(job: dict):
    client         = job["client"]
    ctx            = job["ctx"]
    user_id        = job["user_id"]
    data           = job["state"]
    status_msg     = job["status_msg"]

    audio_msg      = data["audio_msg"]
    download_task  = data["download_task"]
    audio_path     = data["audio_path"]
    cue_path       = data["cue_path"]
    cover_path     = data["cover_path"]
    work_dir       = data["work_dir"]
    chat_id        = data["chat_id"]
    thread_id      = data["thread_id"]
    prompt_msg_id  = data.get("prompt_msg_id")
    art_msg_id     = data.get("art_msg_id")

    output_dir = os.path.join(work_dir, "split")
    os.makedirs(output_dir, exist_ok=True)

    if not status_msg:
        logging.error("CUE missing dynamic status_msg element.")
        return

    await status_msg.edit_text("⚙️ <b>Processing CUE and Audio...</b>", parse_mode=ParseMode.HTML)
    local_thumb    = None

    try:
        # Clean up lingering prompts
        to_delete = [m for m in [prompt_msg_id, art_msg_id] if m]
        if to_delete:
            try:
                await client.delete_messages(chat_id, to_delete)
            except Exception:
                pass

        status_msg = await client.send_message(
            chat_id             = chat_id,
            message_thread_id   = thread_id,
            text                = "⏳ <b>Waiting for download to complete...</b>",
            parse_mode          = ParseMode.HTML,
            reply_to_message_id = audio_msg.id,
        )

        # ── Await the background download (already started) ──
        download_ok = await download_task
        if not download_ok or not os.path.exists(audio_path):
            await status_msg.edit_text("❌ Audio download failed.")
            return

        # Thumbnail for Telegram audio messages
        if cover_path and os.path.exists(cover_path):
            local_thumb = os.path.join(work_dir, "thumbnail.jpg")
            await _generate_thumbnail(cover_path, local_thumb)

        # ── Parse CUE ──
        await status_msg.edit_text("⚙️ <b>Parsing CUE metadata...</b>", parse_mode=ParseMode.HTML)
        cue_data    = _parse_cue_data(cue_path)
        tracks      = cue_data["tracks"]
        global_meta = cue_data["meta"]

        if not tracks:
            await status_msg.edit_text("❌ No TRACK entries found in the CUE file.")
            return

        # ── Split ──
        total_tracks   = len(tracks)
        audio_filename = os.path.basename(audio_path)
        ext            = os.path.splitext(audio_filename)[1].lower()

        # Format normalization
        if ext in (".wv", ".ape"):
            ext = ".flac"
        elif ext == ".alac":
            ext = ".m4a"

        for i, track in enumerate(tracks):
            track_num = i + 1
            title     = track.get("title", f"Track {track_num}")
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
            out_file   = os.path.join(output_dir, f"{track_num:02d} - {safe_title}{ext}")

            start_sec = track["start"]
            end_sec   = tracks[i + 1]["start"] if (i + 1 < len(tracks)) else None

            try:
                await status_msg.edit_text(
                    f"🔪 <b>Splitting</b> track {track_num}/{total_tracks}...",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            cmd = ["ffmpeg", "-y", "-i", audio_path, "-ss", str(start_sec)]
            if end_sec is not None:
                cmd += ["-to", str(end_sec)]
            if ext == ".flac":
                cmd += ["-c:a", "flac"]
            else:
                cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            cmd += ["-map_metadata", "-1", out_file]

            try:
                retcode, stderr = await run_async_subprocess(cmd)
            except Exception as e:
                logger.warning("CUE FFmpeg subprocess fault: %r", e)
                retcode = 1

            if retcode == 0:
                tag_data = {
                    "title":        title,
                    "artist":       track.get("performer", global_meta.get("album_artist", "")),
                    "album":        global_meta.get("album", ""),
                    "genre":        global_meta.get("genre", ""),
                    "date":         global_meta.get("date", ""),
                    "track_number": str(track_num),
                    "total_tracks": str(total_tracks),
                }
                await asyncio.to_thread(_apply_tags, out_file, tag_data, cover_path)
            else:
                logger.warning("FFmpeg non-zero on track %d", track_num)

        # ── Upload ──
        split_files = sorted(glob.glob(os.path.join(output_dir, "*")))
        if not split_files:
            await status_msg.edit_text("❌ Splitting finished but no output files were found.")
            return

        await status_msg.edit_text(
            f"📤 <b>Uploading {len(split_files)} tracks...</b>",
            parse_mode=ParseMode.HTML
        )

        for i, fp in enumerate(split_files):
            try:
                duration    = await _get_duration(fp)
                trk         = tracks[i] if i < len(tracks) else {}
                trk_title   = trk.get("title", os.path.splitext(os.path.basename(fp))[0])
                trk_artist  = trk.get("performer", global_meta.get("album_artist", ""))

                await client.send_audio(
                    chat_id             = chat_id,
                    audio               = fp,
                    title               = trk_title,
                    performer           = trk_artist,
                    thumb               = local_thumb if local_thumb and os.path.exists(local_thumb) else None,
                    duration            = duration,
                    reply_to_message_id = audio_msg.id,
                )
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.error("Upload error for %s: %r", fp, e)

        await status_msg.delete()
        await client.send_message(
            chat_id             = chat_id,
            message_thread_id   = thread_id,
            text                = f"✅ <b>Done!</b> {total_tracks} tracks split and uploaded.",
            parse_mode          = ParseMode.HTML,
            reply_to_message_id = audio_msg.id,
        )

    except Exception:
        logger.exception("CUE splitting critical error")
        try:
            await status_msg.edit_text("❌ <b>Critical error during splitting.</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers (format-agnostic, unchanged logic)
# ---------------------------------------------------------------------------
def _apply_tags(file_path: str, tags: dict, cover_path: Optional[str]) -> None:
    if not HAS_MUTAGEN:
        return
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            for k, tag_key in [("title","title"),("artist","artist"),("album","album"),
                                ("date","date"),("genre","genre")]:
                if tags.get(k): audio[tag_key] = tags[k]
            audio["tracknumber"] = f"{tags['track_number']}/{tags['total_tracks']}"
            if cover_path and os.path.exists(cover_path):
                p = Picture()
                with open(cover_path, "rb") as f: p.data = f.read()
                p.type = 3; p.mime = "image/jpeg"
                audio.add_picture(p)
            audio.save()

        elif ext == ".mp3":
            try: audio = MP3(file_path, ID3=ID3)
            except Exception: audio = MP3(file_path); audio.add_tags()
            if audio.tags is None: audio.add_tags()
            if tags.get("title"):  audio.tags.add(TIT2(encoding=3, text=tags["title"]))
            if tags.get("artist"): audio.tags.add(TPE1(encoding=3, text=tags["artist"]))
            if tags.get("album"):  audio.tags.add(TALB(encoding=3, text=tags["album"]))
            if tags.get("date"):   audio.tags.add(TDRC(encoding=3, text=tags["date"]))
            audio.tags.add(TRCK(encoding=3, text=f"{tags['track_number']}/{tags['total_tracks']}"))
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as f: data = f.read()
                mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            audio.save()

        elif ext in (".m4a", ".mp4"):
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            if tags.get("title"):  audio.tags["\xa9nam"] = [tags["title"]]
            if tags.get("artist"): audio.tags["\xa9ART"] = [tags["artist"]]
            if tags.get("album"):  audio.tags["\xa9alb"] = [tags["album"]]
            if tags.get("date"):   audio.tags["\xa9day"] = [tags["date"]]
            if tags.get("genre"):  audio.tags["\xa9gen"] = [tags["genre"]]
            try: audio.tags["trkn"] = [(int(tags["track_number"]), int(tags["total_tracks"]))]
            except Exception: pass
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as f: data = f.read()
                fmt = MP4Cover.FORMAT_PNG if data.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
                audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()

        elif ext == ".wav":
            try: audio = WAVE(file_path)
            except Exception: return
            if audio.tags is None: audio.add_tags()
            if tags.get("title"):  audio.tags.add(TIT2(encoding=3, text=tags["title"]))
            if tags.get("artist"): audio.tags.add(TPE1(encoding=3, text=tags["artist"]))
            if tags.get("album"):  audio.tags.add(TALB(encoding=3, text=tags["album"]))
            if tags.get("date"):   audio.tags.add(TDRC(encoding=3, text=tags["date"]))
            audio.tags.add(TRCK(encoding=3, text=f"{tags['track_number']}/{tags['total_tracks']}"))
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as f: data = f.read()
                mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            audio.save()

    except Exception as e:
        logger.error("Tagging error on %s: %r", file_path, e)


async def _get_duration(file_path: str) -> int:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return int(float(stdout.decode().strip()))
    except Exception:
        pass
    return 0


async def _generate_thumbnail(input_path: str, output_path: str, size: int = 320):
    try:
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-vf", f"scale='min({size},iw)':'min({size},ih)',format=yuv420p",
               "-vframes", "1", output_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except Exception:
        pass


def _parse_cue_data(cue_path: str) -> dict:
    tracks      = []
    global_meta = {"album": None, "genre": None, "date": None, "album_artist": None}
    current     = {}
    try:
        for enc in ("utf-8-sig", "latin-1"):
            try:
                with open(cue_path, "r", encoding=enc) as f:
                    lines = f.readlines()
                break
            except UnicodeDecodeError:
                continue

        for line in lines:
            line = line.strip()
            if not current:
                if m := re.search(r'PERFORMER\s+"(.*?)"', line):
                    global_meta["album_artist"] = m.group(1)
                if m := re.search(r'TITLE\s+"(.*?)"', line):
                    global_meta["album"] = m.group(1)
                if line.startswith("REM GENRE"):
                    global_meta["genre"] = line.replace("REM GENRE", "").strip().strip('"')
                if line.startswith("REM DATE"):
                    global_meta["date"] = line.replace("REM DATE", "").strip().strip('"')

            if line.startswith("TRACK"):
                if current and "start" in current:
                    current.setdefault("performer", global_meta["album_artist"])
                    tracks.append(current)
                current = {}

            if current is not None:
                if m := re.search(r'TITLE\s+"(.*?)"', line):
                    current["title"] = m.group(1)
                if m := re.search(r'PERFORMER\s+"(.*?)"', line):
                    current["performer"] = m.group(1)
                if m := re.search(r'INDEX 01 (\d+):(\d+):(\d+)', line):
                    mm, ss, ff = map(int, m.groups())
                    current["start"] = mm * 60 + ss + ff / 75.0

        if current and "start" in current:
            current.setdefault("performer", global_meta["album_artist"])
            tracks.append(current)

    except Exception as e:
        logger.error("CUE parse error: %r", e)

    return {"meta": global_meta, "tracks": tracks}