"""Vercel function: publish only the Supabase URL and anon key.

The anon key is meant for the browser. The service role and OpenRouter key
are not read here.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_api import public_supabase_config


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(public_supabase_config(os.environ)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
