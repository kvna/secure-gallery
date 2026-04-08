#!/usr/bin/env python3
"""
Gallery Hub — lists all galleries/, shows running status.
Runs on port 8764.

Usage:
  python3 hub.py
  python3 hub.py --port 8764
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

GALLERIES_DIR = Path(__file__).parent / 'galleries'
HUB_PORT      = 8764
GALLERY_EXTS  = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif',
                 '.tiff', '.tif', '.bmp', '.avif'}


def count_images(photos_dir: Path) -> int:
    if not photos_dir.exists():
        return 0
    return sum(
        1 for f in photos_dir.iterdir()
        if f.is_file() and f.suffix.lower() in GALLERY_EXTS
    )


def check_port(port: int) -> bool:
    try:
        with socket.create_connection(('localhost', port), timeout=0.5):
            return True
    except OSError:
        return False


def get_live_count(port: int):
    try:
        with urllib.request.urlopen(
            f'http://localhost:{port}/api/images/watch', timeout=1
        ) as r:
            return json.loads(r.read()).get('count')
    except Exception:
        return None


def scan_galleries() -> list:
    if not GALLERIES_DIR.exists():
        return []

    results = []
    for entry in sorted(GALLERIES_DIR.iterdir()):
        if not entry.is_dir():
            continue

        name        = entry.name
        description = ''
        port        = 8765

        meta_file = entry / 'gallery.json'
        if meta_file.exists():
            try:
                meta        = json.loads(meta_file.read_text())
                name        = (meta.get('name') or name).strip()
                description = meta.get('description', '')
                port        = int(meta.get('port', 8765))
            except Exception:
                pass

        vault    = (entry / '.vault').exists()
        # Photos live inside the vault mountpoint when vault is enabled
        photos_dir = (entry / 'vault' / 'photos') if vault else (entry / 'photos')
        running    = check_port(port)

        if running:
            live = get_live_count(port)
            image_count = live if live is not None else count_images(photos_dir)
        else:
            image_count = count_images(photos_dir)

        results.append({
            'slug':        entry.name,
            'name':        name,
            'description': description,
            'port':        port,
            'running':     running,
            'image_count': image_count,
            'vault':       vault,
        })

    return results


class HubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logging

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self.serve_file(Path(__file__).parent / 'hub.html', 'text/html; charset=utf-8')
        elif path == '/api/galleries':
            self.send_json(scan_galleries())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        path   = urlparse(self.path).path

        if path == '/api/start':
            slug = body.get('slug', '')
            if not slug:
                self.send_json({'ok': False, 'error': 'No slug'}, 400); return
            gallery_dir = GALLERIES_DIR / slug
            if not gallery_dir.exists():
                self.send_json({'ok': False, 'error': 'Gallery not found'}, 404); return
            try:
                subprocess.Popen(
                    ['bash', os.path.expanduser('~/start-gallery.sh'), slug],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)

        elif path == '/api/stop':
            port = body.get('port', 0)
            if not port:
                self.send_json({'ok': False, 'error': 'No port'}, 400); return
            try:
                result = subprocess.run(
                    ['lsof', '-ti', f':{port}'],
                    capture_output=True, text=True
                )
                pids = [p for p in result.stdout.strip().split() if p.isdigit()]
                for pid in pids:
                    os.kill(int(pid), signal.SIGTERM)
                self.send_json({'ok': True, 'killed': pids})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()


def run(port: int):
    GALLERIES_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(('localhost', port), HubHandler)
    print(f"\n🗂  Gallery Hub")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"   URL      : http://localhost:{port}")
    print(f"   Galleries: {GALLERIES_DIR}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"\nOpen http://localhost:{port} in your browser")
    print("Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHub stopped.")


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Gallery Hub')
    p.add_argument('--port', '-p', type=int, default=HUB_PORT)
    a = p.parse_args()
    run(a.port)
