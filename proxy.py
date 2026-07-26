#!/usr/bin/env python3
"""
HTTP Proxy: OpenAI /v1/chat/completions → Ollama /api/chat

Empfängt OpenAI-kompatible Requests, übersetzt sie mit translator.py
ins native Ollama-Format, leitet weiter, übersetzt die Antwort zurück
und streamt SSE an den Client.

Port / Ollama-URL / Config via Umgebungsvariablen.
"""
import json
import os
import sys
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests

from translator import translate_request, translate_response, format_sse

# ── Konfiguration ────────────────────────────────────────────────

HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("BRIDGE_PORT", "8080"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.0.42:18080") # "http://192.168.0.42:11434"

# Konfiguration aus .env: Alle DEFAULT_* Variablen.
# Nur gesetzte Werte werden in den Request injiziert.
BRIDGE_CONFIG = {
    "num_ctx": int(os.getenv("DEFAULT_NUM_CTX", "32768")),
    "keep_alive": os.getenv("DEFAULT_KEEP_ALIVE", "30m"),
}

# Optionale Sampling-Parameter – nur wenn in .env gesetzt
for key, env, typ in [
    ("num_predict", "DEFAULT_NUM_PREDICT", int),
    ("temperature", "DEFAULT_TEMPERATURE", float),
    ("top_p", "DEFAULT_TOP_P", float),
    ("top_k", "DEFAULT_TOP_K", int),
    ("min_p", "DEFAULT_MIN_P", float),
    ("repeat_penalty", "DEFAULT_REPEAT_PENALTY", float),
    ("frequency_penalty", "DEFAULT_FREQUENCY_PENALTY", float),
    ("presence_penalty", "DEFAULT_PRESENCE_PENALTY", float),
    ("seed", "DEFAULT_SEED", int),
    ("repeat_last_n", "DEFAULT_REPEAT_LAST_N", int),
    ("stop", "DEFAULT_STOP", str),
]:
    val = os.getenv(env)
    if val is not None:
        if env == "DEFAULT_STOP":
            BRIDGE_CONFIG[key] = json.loads(val) if val.startswith("[") else val
        else:
            BRIDGE_CONFIG[key] = typ(val)


# ── Request Handler ──────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        """Structured logging: Timestamp, Methode, Pfad, Status, Dauer."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] {args[0]} {args[1]} → {args[2]}\n")

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        self._proxy_pass("GET")

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self._proxy_pass("POST")

    def _proxy_pass(self, method):
        """Leitet alle Nicht-Completions 1:1 an Ollama weiter (z.B. /api/tags)."""
        path = self.path
        target = f"{OLLAMA_URL}{path}"

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""

            resp = requests.request(
                method, target,
                data=body,
                headers={k: v for k, v in self.headers.items()
                         if k.lower() not in ("host", "content-length", "accept-encoding")},
                stream=True,
                timeout=(10, 300),
            )

            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(k, v)
            self._send_cors()
            self.end_headers()

            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
            self.wfile.flush()

        except requests.exceptions.ConnectionError:
            self.send_response(502)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Ollama nicht erreichbar"}).encode())

    def _handle_chat_completions(self):
        """POST /v1/chat/completions – der Haupt-Endpoint."""
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        try:
            openai_req = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_error(400, "Ungültiges JSON")
            return

        streaming = openai_req.get("stream", False)

        # ── 01 → 02 (Request-Translation) ───────────────────────
        start = time.time()
        ollama_req = translate_request(openai_req, BRIDGE_CONFIG)
        model = ollama_req.get("model", "?")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] POST /v1/chat/completions model={model} stream={streaming} "
                         f"options={ollama_req.get('options')}\n")

        # ── An Ollama senden ─────────────────────────────────────
        try:
            ollama_resp = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json=ollama_req,
                stream=True,
                timeout=(10, 600),
            )
        except requests.exceptions.ConnectionError:
            self._send_error(502, "Ollama nicht erreichbar")
            return
        except requests.exceptions.Timeout:
            self._send_error(504, "Ollama Zeitüberschreitung")
            return

        if ollama_resp.status_code != 200:
            err_body = ollama_resp.text
            self._send_error(502, f"Ollama Fehler: {err_body}")
            return

        # ── 03 → 04 (Response-Translation) ──────────────────────
        # Intern immer streamen, Ausgabe je nach Client-Wunsch
        def iter_ollama_chunks():
            for line in ollama_resp.iter_lines():
                if line:
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        sys.stderr.write(f"[{ts}] WARN: Ungültiges JSON von Ollama: {line[:200]!r}\n")

        def _write_sse(event, done=False):
            if done:
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            last_event = None
            if streaming:
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.flush()

                wrote_done = False
                try:
                    for sse_event in translate_response(iter_ollama_chunks()):
                        last_event = sse_event
                        if sse_event is None:
                            _write_sse(None, done=True)
                            wrote_done = True
                        else:
                            _write_sse(sse_event)
                except Exception:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    sys.stderr.write(f"[{ts}] FEHLER im SSE-Stream: {traceback.format_exc()}\n")
                    _write_sse(None, done=True)
                    wrote_done = True
                finally:
                    if not wrote_done:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()

                # Verbindung schließen – sonst blockiert Keep-Alive
                self.close_connection = True

            else:
                # Non-Streaming: alle Chunks sammeln → komplettes OpenAI-Response bauen
                content_parts = []
                reasoning_parts = []
                tool_calls = None
                usage = None
                finish_reason = None
                for sse_event in translate_response(iter_ollama_chunks()):
                    last_event = sse_event
                    if sse_event is None:
                        break
                    choices = sse_event.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        if delta.get("reasoning"):
                            reasoning_parts.append(delta["reasoning"])
                        if delta.get("tool_calls"):
                            tool_calls = delta["tool_calls"]
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                    if sse_event.get("usage"):
                        usage = sse_event["usage"]

                content = "".join(content_parts) if content_parts else ""
                if not content and reasoning_parts:
                    content = "".join(reasoning_parts)

                message = {"role": "assistant", "content": content}
                if reasoning_parts and content_parts:
                    message["reasoning"] = "".join(reasoning_parts)
                if tool_calls:
                    message["tool_calls"] = tool_calls

                ref = last_event or {}
                resp_body = {
                    "id": ref.get("id", "chatcmpl-0"),
                    "object": "chat.completion",
                    "created": ref.get("created", int(time.time())),
                    "model": ref.get("model", model),
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason or "stop",
                    }],
                }
                if usage:
                    resp_body["usage"] = usage

                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp_body, ensure_ascii=False).encode())
                self.wfile.flush()

            duration = time.time() - start
            sys.stderr.write(f"[{ts}] {'SSE' if streaming else 'JSON'} → 200  ({duration:.2f}s)\n")

        except Exception:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            sys.stderr.write(f"[{ts}] FEHLER in Response-Translation: {traceback.format_exc()}\n")
            try:
                self._send_error(500, "Interner Fehler bei der Response-Übersetzung")
            except Exception:
                pass

    def _send_error(self, code, msg):
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())


# ── Server-Start ────────────────────────────────────────────────

def main():
    server = HTTPServer((HOST, PORT), BridgeHandler)
    print(f"py-ollama-openai-bridge läuft auf http://{HOST}:{PORT}")
    print(f"Ollama Backend: {OLLAMA_URL}")
    print(f"num_ctx: {BRIDGE_CONFIG['num_ctx']}, num_predict: {BRIDGE_CONFIG.get('num_predict', 'client-default (32000)')}, keep_alive: {BRIDGE_CONFIG['keep_alive']}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer gestoppt.")
        server.server_close()


if __name__ == "__main__":
    main()
