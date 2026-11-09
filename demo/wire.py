#!/usr/bin/env python3
"""The demo "wire": a loopback receiver that prints every post as big teletype text.

Usage: python3 demo/wire.py [--port 8765] [--log wire.log]
Listens on 127.0.0.1 only. Nothing leaves the machine. Output is the same for the same post.
"""
import argparse
import json
import textwrap
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

COUNT = 0


def teletype(text: str) -> str:
    wide = [" ".join(line) for line in textwrap.wrap(text.upper(), 22) or [""]]  # one space between letters: reads big on a recording
    bar = "=" * max(len(w) for w in wide + ["INCOMING WIRE"])
    return "\n".join(["", bar, "INCOMING WIRE", bar, *wide, bar, ""])


def make_handler(log_path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"wire ok\n")

        def do_POST(self):
            global COUNT
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
            ctype = self.headers.get("Content-Type", "")
            text = " ".join(parse_qs(raw).get("text", [raw])) if "urlencoded" in ctype else raw
            COUNT += 1
            print(teletype(text), flush=True)
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"n": COUNT, "text": text}) + "\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"on the wire\n")

        def log_message(self, *a):  # keep the terminal for the teletype
            pass
    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    print(f"wire listening on http://127.0.0.1:{a.port} (empty until something is posted)", flush=True)
    HTTPServer(("127.0.0.1", a.port), make_handler(a.log)).serve_forever()


if __name__ == "__main__":
    main()
