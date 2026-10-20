#!/usr/bin/env python3
"""Serve the dashboard locally with recorded demo data.

    python3 serve_demo.py
    open http://localhost:8765/?demo=1
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent / "dashboard"
MOCK = False
SOURCE = ""


def read_demo() -> dict:
    return json.loads((ROOT / "demo.json").read_text(encoding="utf-8"))


def source_json(path: str) -> dict:
    url = SOURCE.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def mock_data() -> dict:
    if SOURCE:
        try:
            return source_json("/data")
        except Exception:
            pass
    demo = read_demo()
    data = copy.deepcopy(demo.get("current") or demo["timeline"][0]["data"])
    now = time.time()
    data["now"] = now
    data["last_update"] = now - 0.5
    data["status"]["ts"] = now - 0.5
    data["fanmode"]["ts"] = now - 0.5
    data["blow"]["ts"] = now - 0.5
    data["e9"]["ts"] = now - 0.5
    data["mock"] = True
    return data


def mock_buslog() -> dict:
    if SOURCE:
        try:
            return source_json("/buslog?n=250")
        except Exception:
            pass
    demo = read_demo()
    frames = []
    for item in demo["timeline"]:
        for frame in item["data"].get("frames", []):
            frames.append([frame["ts"], frame["hex"].replace(" ", "")])
    return {"now": time.time(), "count": len(frames), "frames": frames[-200:], "mock": True}


def mock_results() -> dict:
    if SOURCE:
        try:
            return source_json("/results")
        except Exception:
            pass
    return {"results": [], "mock": True}


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if MOCK and path == "/data":
            self.send_json(mock_data())
            return
        if MOCK and path == "/buslog":
            self.send_json(mock_buslog())
            return
        if MOCK and path == "/results":
            self.send_json(mock_results())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not MOCK:
            self.send_json({"ok": False, "err": "POST is only available in mock/proxy mode"}, 405)
            return
        if not SOURCE:
            self.send_json({"ok": False, "err": "recorded mock mode: recording is disabled"}, 503)
            return
        try:
            request = urllib.request.Request(SOURCE.rstrip("/") + self.path, data=b"", method="POST")
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        except Exception as error:
            self.send_json({"ok": False, "err": str(error)}, 502)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[demo] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Gree XK19 dashboard demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mock", action="store_true", help="Serve recorded data at /data for live-mode UI testing")
    parser.add_argument("--source", default="", help="Proxy /data, /buslog, /results and POSTs to a live server")
    args = parser.parse_args()
    global MOCK, SOURCE
    SOURCE = args.source.rstrip("/")
    MOCK = args.mock or bool(SOURCE)
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    suffix = "" if MOCK else "/?demo=1"
    print(f"Dashboard: http://{args.host}:{args.port}{suffix}")
    if MOCK:
        if SOURCE:
            print(f"Proxy mode: /data, /buslog and /results are proxied from {SOURCE}")
        else:
            print("Mock mode: /data and /buslog are served from the recorded current snapshot.")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
