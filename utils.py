import math
import time
from typing import Optional
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified, FloodWait
import asyncio

async def progress_callback(
    current: int,
    total: int,
    message: Message,
    action_text: str,
    start_time: float,
    last_edit_time: list[float]
):
    """
    A progress callback for Pyrogram's upload/download methods.
    Rate-limits message edits to avoid FloodWaits.
    """
    now = time.time()
    # Edit at most once every 1.5 seconds, or when we hit 100%
    if now - last_edit_time[0] < 1.5 and current < total:
        return

    last_edit_time[0] = now
    
    # Handle unknown total sizes
    if total == 0:
        total = current + 1 

    percent = current * 100 / total
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    
    # Build progress bar UI [██████░░░░]
    filled_blocks = int(math.floor((percent / 100.0) * 10))
    empty_blocks = 10 - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks

    # Convert bytes to MB
    current_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    speed_mb = speed / (1024 * 1024)

    text = (
        f"⏳ <b>{action_text}</b>\n\n"
        f"<code>{bar}</code> {percent:.1f}%\n"
        f"<b>{current_mb:.1f} MB</b> of <b>{total_mb:.1f} MB</b>\n"
        f"🚀 <b>{speed_mb:.1f} MB/s</b>"
    )

    try:
        await message.edit_text(text, parse_mode=message.client.parse_mode)
    except MessageNotModified:
        pass
    except FloodWait as e:
        last_edit_time[0] += e.value  # Sleep implicitly by delaying next edit limit
    except Exception:
        pass
