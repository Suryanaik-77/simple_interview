"""
OpenAI Realtime API 2.1-mini — Test Server
WebSocket relay: browser <-> this server <-> OpenAI Realtime API.
Keeps the API key server-side. Also serves the HTML UI via HTTP.
"""

import os
import json
import asyncio
import http.server
import threading
import websockets

import interview_prep

DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(DIR, "..", ".env")

def load_env():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
HTTP_PORT = int(os.environ.get("REALTIME_HTTP_PORT", "8100"))
WS_PORT = int(os.environ.get("REALTIME_WS_PORT", "8101"))
MODEL = "gpt-realtime-2.1-mini"
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"


# ── HTTP server (serves index.html) ─────────────────────────────────────────

class HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(DIR, "index.html"), "rb") as f:
                data = f.read()
            self._send(200, data, "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/prepare":
            self._handle_prepare()
        elif path == "/api/extract_resume":
            self._handle_extract_resume()
        else:
            self.send_error(404)

    def _handle_extract_resume(self):
        """Extract plain text from an uploaded resume file so the candidate can
        upload instead of pasting. Body = raw file bytes; ?filename=<name.ext>.
        Returns {text, chars}. The UI drops the text into the resume box and the
        normal prepare flow parses it."""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            filename = (qs.get("filename") or [""])[0]
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length) if length else b""
        except Exception as e:
            return self._json(400, {"error": f"bad upload: {e}"})
        if not data:
            return self._json(400, {"error": "empty file"})
        text = interview_prep.extract_resume_text(filename, data)
        if not text or len(text.strip()) < 20:
            return self._json(422, {
                "error": "Couldn't read text from this file (it may be a scanned "
                         "image). Try a text-based PDF/DOCX, or paste the resume."})
        return self._json(200, {"text": text, "chars": len(text),
                                "filename": filename})

    def _handle_prepare(self):
        """Build the interviewer system prompt from resume + previous 2 sessions.

        Body: {email, resume_text?, and/or structured resume fields
               (candidate_name, level, domain, years_experience, tools, skills,
                key_projects)}.  Any resume_text is parsed and then overridden by
        explicitly-supplied fields.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad request: {e}"})

        email = (body.get("email") or "").strip()
        resume = {}
        resume_text = (body.get("resume_text") or "").strip()
        if resume_text:
            resume = interview_prep.parse_resume(resume_text)
            resume["resume_text"] = resume_text

        # Explicit fields override anything parsed.
        for k in ("candidate_name", "level", "domain", "years_experience",
                  "tools", "skills", "key_projects"):
            if body.get(k) not in (None, "", []):
                resume[k] = body[k]

        try:
            result = interview_prep.build_instructions(resume, email)
        except Exception as e:
            return self._json(500, {"error": f"build failed: {e}"})

        return self._json(200, {
            "instructions": result["instructions"],
            "meta": result["meta"],
            "resume": {k: resume.get(k) for k in
                       ("candidate_name", "level", "domain", "years_experience",
                        "tools", "skills", "key_projects")},
        })

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def run_http():
    server = http.server.HTTPServer(("0.0.0.0", HTTP_PORT), HTTPHandler)
    print(f"[HTTP] Serving UI at http://0.0.0.0:{HTTP_PORT}")
    server.serve_forever()


# ── WebSocket relay ──────────────────────────────────────────────────────────

async def relay(client_ws):
    """Relay messages between a browser client and OpenAI Realtime API."""
    print(f"[WS] Client connected")

    extra_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    try:
        async with websockets.connect(
            OPENAI_WS_URL,
            additional_headers=extra_headers,
            max_size=10 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=10,
        ) as openai_ws:
            print(f"[WS] Connected to OpenAI Realtime API ({MODEL})")

            async def client_to_openai():
                try:
                    async for msg in client_ws:
                        await openai_ws.send(msg)
                except websockets.ConnectionClosed:
                    pass

            async def openai_to_client():
                try:
                    async for msg in openai_ws:
                        await client_ws.send(msg)
                except websockets.ConnectionClosed:
                    pass

            await asyncio.gather(
                client_to_openai(),
                openai_to_client(),
            )

    except Exception as e:
        print(f"[WS] Error: {e}")
        try:
            await client_ws.send(json.dumps({
                "type": "error",
                "error": {"message": str(e), "type": "relay_error"}
            }))
        except:
            pass

    print(f"[WS] Client disconnected")


async def ws_server():
    print(f"[WS] Relay listening on ws://0.0.0.0:{WS_PORT}")
    async with websockets.serve(relay, "0.0.0.0", WS_PORT, max_size=10 * 1024 * 1024):
        await asyncio.Future()


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    print(f"OpenAI key: {OPENAI_API_KEY[:12]}..." if OPENAI_API_KEY else "WARNING: No OPENAI_API_KEY!")
    print(f"Model: {MODEL}")

    # Warm the candidate-history backend so the first /api/prepare is fast and we
    # log which store (DB vs file) is in use.
    db = interview_prep._get_db()
    print(f"[prep] history store: {'postgres' if db else 'file/none'}")

    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()

    asyncio.run(ws_server())
