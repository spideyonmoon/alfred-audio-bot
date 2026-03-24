"""
health.py — Minimal HTTP health server for HuggingFace Spaces.

HF Docker Spaces expect something listening on port 7860 to show as "Running".
This runs a tiny asyncio HTTP server alongside the bot that returns 200 OK.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

async def _handle(reader, writer):
    await reader.read(1024)
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 2\r\n"
        b"\r\n"
        b"OK"
    )
    writer.write(response)
    await writer.drain()
    writer.close()

async def start_health_server(port: int = 7860):
    server = await asyncio.start_server(_handle, "0.0.0.0", port)
    addr = server.sockets[0].getsockname()
    logger.info("Health server listening on %s:%s", addr[0], addr[1])
    async with server:
        await server.serve_forever()
