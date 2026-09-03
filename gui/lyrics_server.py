import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

def find_available_port(start_port: int, tries: int = 100):
    for i in range(tries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", start_port + i))
            s.close()
            return start_port + i
        except OSError:
            pass
    raise Exception(
        f"No available port from {start_port} to {start_port + tries}."
    )

class LyricsSaveHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/save":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(post_data)
                self.server.app_window.web_lyrics_saved.emit(
                    data['track_id'], data['language'], data['lyrics_text']
                )
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            except Exception as e:
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logging

_global_server = None
_global_port = None

def ensure_lyrics_server(app_window: Any, start_port) -> int:
    global _global_server, _global_port
    if _global_server is None:
        port = find_available_port(start_port)
        _global_server = HTTPServer(('127.0.0.1', port), LyricsSaveHandler)
        _global_server.app_window = app_window
        _global_port = port
        thread = threading.Thread(target=_global_server.serve_forever, daemon=True)
        thread.start()
    else:
        _global_server.app_window = app_window
    return _global_port

def stop_lyrics_server():
    global _global_server, _global_port
    if _global_server is not None:
        _global_server.shutdown()
        _global_server.server_close()
        _global_server = None
        _global_port = None
