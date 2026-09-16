import asyncio
import http.server
import os
import socketserver
import logging

logger = logging.getLogger(__name__)

# HuggingFace Spaces' documented app port, used when the platform injects nothing.
DEFAULT_PORT = 7860

def resolve_port() -> int:
    """The port this platform expects the app to listen on.

    Render (like Heroku and other PaaS) injects ``PORT`` and routes traffic to it, so a
    hard-coded port leaves the app unbound from the router's point of view and its health
    probe fails. HuggingFace Spaces sets no such variable and uses 7860. ``HEALTH_PORT``
    overrides both for local or manual runs.
    """
    for var in ("HEALTH_PORT", "PORT"):
        raw = os.getenv(var, "").strip()
        if raw.isdigit():
            return int(raw)
    return DEFAULT_PORT

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/crash':
            try:
                with open('/tmp/crash.log', 'r', encoding='utf-8') as f:
                    msg = f.read()
            except Exception:
                msg = 'No crash log found.'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(msg.encode('utf-8'))
        else:
            # Every other path answers 200: platforms probe "/", "/health", or nothing
            # at all, and each of those just means "is the process up".
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Alfred OK')

    def log_message(self, format, *args):
        pass  # suppress access logs to avoid spamming the console

def _run_server(port):
    # allow_reuse_address prevents "Address already in use" if the container crashes rapidly
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
        logger.info("Health server listening on 0.0.0.0:%d", port)
        httpd.serve_forever()

async def start_health_server(port: int = 0):
    await asyncio.to_thread(_run_server, port or resolve_port())
