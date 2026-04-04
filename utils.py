import math
import time
from typing import Optional
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified, FloodWait
import asyncio
from pyrogram.enums import ParseMode

def time_formatter(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d ") if days else "") + \
        ((str(hours) + "h ") if hours else "") + \
        ((str(minutes) + "m ") if minutes else "") + \
        ((str(seconds) + "s") if seconds else "")
    return tmp if tmp else "0s"

def humanbytes(size: float) -> str:
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{str(round(size, 2))} {Dic_powerN[n]}B"

async def progress_callback(
    current: int,
    total: int,
    status_msg: Message,
    prefix: str,
    start_time: float,
    last_update: list[float],
    job_id: str = None
):
    """
    Standard Pyrogram download/upload progress tracker with injected 
    telemetry mapping to support WZML-style global queues.
    """
    now = time.time()
    
    # Throttle API edit requests to every 3 seconds to avoid FloodWait
    if now - last_update[0] < 3.0 and current < total:
        return
        
    last_update[0] = now
    
    percent = (current / total) * 100 if total > 0 else 0
    speed   = current / (now - start_time) if now > start_time else 0
    eta     = (total - current) / speed if speed > 0 else 0

    speed_str = humanbytes(speed) + "/s"
    eta_str   = time_formatter(eta)
    curr_str  = humanbytes(current)
    tot_str   = humanbytes(total)
    
    # Intercept telemetry values globally if a job_id context exists
    try:
        import sys
        if "bot" in sys.modules:
            _active_jobs = sys.modules["bot"]._active_jobs
            if job_id and job_id in _active_jobs:
                job_state = _active_jobs[job_id]
                job_state["progress"] = percent
                job_state["speed"] = speed_str
                job_state["eta"] = eta_str
                job_state["downloaded"] = curr_str
                job_state["total"] = tot_str
                job_state["status"] = f"{prefix}"
    except Exception:
        pass
    
    text = (
        f"<b>{prefix}</b>\n"
        f"┠ <b>Progress:</b> <code>{percent:.1f}%</code>\n"
        f"┠ <b>Size:</b> <code>{curr_str} / {tot_str}</code>\n"
        f"┠ <b>Speed:</b> <code>{speed_str}</code>\n"
        f"┖ <b>ETA:</b> <code>{eta_str}</code>"
    )
    
    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def run_async_subprocess(cmd: list) -> tuple[int, str]:
    """
    Native asynchronous subprocess dispatcher designed specifically to pass 
    `CancelledError` exceptions into the internal child processes, forcing
    abort termination against FFmpeg and SoX OS threads reliably.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await proc.communicate()
        return proc.returncode, stderr.decode(errors="replace")
    except asyncio.CancelledError:
        proc.kill()
        raise
