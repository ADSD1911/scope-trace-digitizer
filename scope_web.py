#!/usr/bin/env python3
"""Scope Trace Digitizer in the browser, run from this Mac.

    python3 scope_web.py              start it and open the page
    python3 scope_web.py --make-site  write the "docs" folder: the version that needs no server,
                                      ready for GitHub Pages or any other static web host

Started this way, Python on this Mac does the reading (with the Mac's own text recogniser) and
the page is its face. Everything stays on this computer; the page works for as long as this
program is running.
"""
import argparse
import json
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scope_api

HERE = Path(__file__).resolve().parent
LOCK = threading.Lock()  # one request is worked on at a time
SITE_FILES = {"index.html": "scope_page.html", "scope_digitizer.py": "scope_digitizer.py", "scope_api.py": "scope_api.py"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the Terminal window quiet
        pass

    def reply(self, status, kind, payload, filename=None):
        if not isinstance(payload, (bytes, bytearray)):
            payload = (json.dumps(payload) if kind == "application/json" else str(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") or kind == "application/json" else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def answer(self, method):
        try:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > scope_api.MAX_UPLOAD:
                return self.reply(413, "application/json", {"error": "That file is too large."})
            body = self.rfile.read(length) if length else b""
            if method == "GET" and url.path in ("/", "/index.html"):
                return self.reply(200, "text/html", (HERE / "scope_page.html").read_bytes())
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            with LOCK:
                self.reply(*scope_api.handle(method, url.path, query, body))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:  # report the problem to the page instead of dropping the connection
            sys.stderr.write(f"Error on {self.path}: {err!r}\n")
            try:
                self.reply(500, "application/json", {"error": f"{type(err).__name__}: {err}"})
            except OSError:
                pass

    def do_GET(self):
        self.answer("GET")

    def do_POST(self):
        self.answer("POST")


def make_site(target):
    target.mkdir(parents=True, exist_ok=True)
    for name, source in SITE_FILES.items():
        shutil.copyfile(HERE / source, target / name)
    (target / ".nojekyll").touch()  # GitHub Pages: serve the files as they are
    print(f"Wrote {len(SITE_FILES)} files to {target}")
    print("Upload that folder to any static web host. The page then runs entirely in each visitor's browser.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    p.add_argument("--make-site", nargs="?", const=str(HERE / "docs"), metavar="FOLDER", help="write the no-server version and stop")
    args = p.parse_args()
    if args.make_site:
        return make_site(Path(args.make_site))
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # this computer only
            break
        except OSError:
            continue
    if not server:
        sys.exit(f"Ports {args.port} to {args.port + 9} are all in use.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Scope Trace Digitizer is running at {url}")
    print("Keep this window open while you use the page. Close it, or press Control-C, to stop.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
