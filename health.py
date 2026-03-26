"""
health.py — Minimal HTTP health server for HuggingFace Spaces.

HF Docker Spaces expect something responding on port 7860.
This runs a tiny asyncio HTTP server alongside the bot that returns 200 OK.
If not on HuggingFace, this file is imported but never called.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 8\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"Alfred OK"
)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        request = await reader.read(1024)  # consume the incoming HTTP request
        req_str = request.decode("utf-8", errors="ignore")
        if "GET /crash" in req_str:
            import os
            if os.path.exists("/tmp/crash.log"):
                with open("/tmp/crash.log", "r", encoding="utf-8") as f:
                    err = f.read()
                resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n{err}".encode("utf-8")
                writer.write(resp)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nNo crash log found.")
        else:
            writer.write(_RESPONSE)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server(port: int = 7860):
    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    addr = server.sockets[0].getsockname()
    logger.info("Health server listening on %s:%s", addr[0], addr[1])
    async with server:
        await server.serve_forever()
