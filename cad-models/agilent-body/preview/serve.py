"""Static CAD preview, bound only to this host's Tailscale address."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIST = Path(__file__).resolve().parent / 'dist'

class Handler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(404)
        return None

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

if __name__ == '__main__':
    server = ThreadingHTTPServer(('100.64.254.6', 8765), partial(Handler, directory=str(DIST)))
    print('CAD preview: http://100.64.254.6:8765', flush=True)
    server.serve_forever()
