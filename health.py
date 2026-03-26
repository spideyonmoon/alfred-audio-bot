import asyncio
import http.server
import socketserver
import logging

logger = logging.getLogger(__name__)

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

async def start_health_server(port: int = 7860):
    await asyncio.to_thread(_run_server, port)
