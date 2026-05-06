"""
Standalone server for the presentation page.

Usage:
    python serve_presentation.py

Then open: http://127.0.0.1:8080
"""

import os
import http.server
import socketserver

PORT = 8080
PRESENTATION_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "chord", "static", "presentation"
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PRESENTATION_DIR, **kwargs)

    def log_message(self, format, *args):
        print(f"  {self.address_string()} — {format % args}")


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"\n  Presentation server running.")
        print(f"  Open: http://127.0.0.1:{PORT}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")
