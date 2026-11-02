"""Vercel function: run a paid-style analysis request.

OpenRouter and the Supabase service role stay in the server environment.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_api import HttpTransport, analyze_configured, run_analysis


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        _send(self, 200, {"configured": analyze_configured(os.environ)})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            _send(self, 400, {"error": "The request body is not valid JSON."})
            return
        status, payload = run_analysis(os.environ, dict(self.headers), body, HttpTransport())
        _send(self, status, payload)
