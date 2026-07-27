#!/usr/bin/env python3
"""
HTTP Proxy: OpenAI /v1/chat/completions -> Ollama /api/chat

Empfängt OpenAI-kompatible Requests, uebersetzt sie mit translator.py
ins native Ollama-Format, leitet weiter, uebersetzt die Antwort zurueck
und streamt SSE an den Client.

Failover: OLLAMA_URL -> FAILOVER_OLLAMA_URL (bei ConnectionError/Timeout)
"""
import errno
import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse

import requests

from translator import translate_request, translate_response


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t = threading.current_thread()
    name = t.name
    if name.startswith("Thread-"):
        num = name.split()[0][7:]
        tid = f"T{num}"
    else:
        tid = name[:6]
    sys.stderr.write(f"[{tid}] [{ts}] {msg}\n")


def _target_hostport(target: Optional['Target']) -> str:
    if target is None:
        return "?"
    p = urlparse(target.url)
    host = p.hostname or target.url
    port = f":{p.port}" if p.port else ""
    return f"{host}{port}"


# ── Target-Konfiguration ───────────────────────────────────────

@dataclass
class Target:
    name: str
    url: str
    num_ctx: Optional[int] = None
    num_predict: Optional[int] = None
    keep_alive: Optional[str] = None
    timeout: int = 5


def _int_or_none(val: Optional[str]) -> Optional[int]:
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return None


def _parse_targets() -> list[Target]:
    targets = []

    ollama_url = os.getenv("OLLAMA_URL")
    if ollama_url:
        targets.append(Target(name="ollama", url=ollama_url))

    failover_url = os.getenv("FAILOVER_OLLAMA_URL")
    if failover_url:
        targets.append(Target(
            name="failover",
            url=failover_url,
            num_ctx=_int_or_none(os.getenv("FAILOVER_NUM_CTX")),
            num_predict=_int_or_none(os.getenv("FAILOVER_NUM_PREDICT")),
            keep_alive=os.getenv("FAILOVER_KEEP_ALIVE"),
            timeout=int(os.getenv("FAILOVER_TIMEOUT", "10")),
        ))

    return targets


def _build_effective_config(base: dict, target: Target) -> dict:
    cfg = dict(base)
    if target.num_ctx is not None:
        cfg["num_ctx"] = target.num_ctx
    if target.num_predict is not None:
        cfg["num_predict"] = target.num_predict
    if target.keep_alive is not None:
        cfg["keep_alive"] = target.keep_alive
    return cfg


# ── Konfiguration aus .env ─────────────────────────────────────

HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("BRIDGE_PORT", "8080"))

_all_targets = _parse_targets()
if not _all_targets:
    sys.exit("FEHLER: Kein Ollama-Target konfiguriert. Setze OLLAMA_URL oder FAILOVER_OLLAMA_URL in .env.")

BRIDGE_CONFIG = {
    "num_ctx": int(os.getenv("NUM_CTX", "32768")),
    "keep_alive": os.getenv("KEEP_ALIVE", "30m"),
}

for key, env, typ in [
    ("num_predict", "NUM_PREDICT", int),
    ("temperature", "TEMPERATURE", float),
    ("top_p", "TOP_P", float),
    ("top_k", "TOP_K", int),
    ("min_p", "MIN_P", float),
    ("repeat_penalty", "REPEAT_PENALTY", float),
    ("frequency_penalty", "FREQUENCY_PENALTY", float),
    ("presence_penalty", "PRESENCE_PENALTY", float),
    ("seed", "SEED", int),
    ("repeat_last_n", "REPEAT_LAST_N", int),
    ("stop", "STOP", str),
]:
    val = os.getenv(env)
    if val is not None:
        if env == "STOP":
            BRIDGE_CONFIG[key] = json.loads(val) if val.startswith("[") else val
        else:
            BRIDGE_CONFIG[key] = typ(val)


# ── Request Handler ────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.path = ''
                return False
            if not self.raw_requestline:
                self.close_connection = True
                return False
            if not self.parse_request():
                return False
            mname = 'do_' + self.command
            if not hasattr(self, mname):
                self.send_error(501, "Unsupported method (%r)" % self.command)
                return False
            method = getattr(self, mname)
            method()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log(f"Client disconnected ({self.client_address[0]}:{self.client_address[1]})")
            self.close_connection = True
            return False
        except TimeoutError as e:
            self.log_error("Request timed out: %r", e)
            return False
        except OSError as e:
            if e.errno == errno.EPIPE:
                _log(f"Client disconnected ({self.client_address[0]}:{self.client_address[1]})")
                self.close_connection = True
                return False
            raise
        except:
            self.handle_exception()
        return True

    def log_request(self, code='-', size='-'):
        target = getattr(self, '_current_target', None)
        hp = _target_hostport(target)
        _log(f"[{hp}] {self.command} {self.path} -> {code}")

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        _log(f"[?] incoming: {self.command} {self.path}")
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        _log(f"[?] incoming: {self.command} {self.path}")
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self._proxy_pass("GET")

    def do_POST(self):
        _log(f"[?] incoming: {self.command} {self.path}")
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self._proxy_pass("POST")

    # ── Target Selection ─────────────────────────────────────────

    def _select_target(self, exclude_urls: Optional[set] = None) -> Optional[Target]:
        targets = _parse_targets()
        exclude = exclude_urls or set()
        for target in targets:
            if target.url in exclude:
                continue
            try:
                resp = requests.get(f"{target.url}/api/tags", timeout=target.timeout)
                if resp.status_code == 200:
                    return target
            except Exception:
                continue
        return None

    # ── Non-Chat Forwarding mit Failover ─────────────────────────

    def _proxy_pass(self, method):
        path = self.path
        failed = set()

        while True:
            target = self._select_target(failed)
            if target is None:
                self._send_error(502, "Kein Ollama verfuegbar")
                return

            self._current_target = target

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""

                resp = requests.request(
                    method, f"{target.url}{path}",
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
                return

            except requests.exceptions.ConnectionError:
                failed.add(target.url)
                _log(f"[{_target_hostport(target)}] ConnectionError in proxy_pass -> failover")
                continue

    # ── Chat Completions mit Failover ────────────────────────────

    def _handle_chat_completions(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        try:
            openai_req = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_error(400, "Ungueltiges JSON")
            return

        streaming = openai_req.get("stream", False)
        failed_urls = set()

        while True:
            target = self._select_target(failed_urls)
            if target is None:
                self._send_error(502, "Kein Ollama verfuegbar")
                return

            self._current_target = target
            effective_config = _build_effective_config(BRIDGE_CONFIG, target)

            start = time.time()
            ollama_req = translate_request(openai_req, effective_config)
            model = ollama_req.get("model", "?")
            _log(f"[{_target_hostport(target)}] POST /v1/chat/completions model={model} stream={streaming} "
                 f"options={ollama_req.get('options')}")

            try:
                ollama_resp = requests.post(
                    f"{target.url}/api/chat",
                    json=ollama_req,
                    stream=True,
                    timeout=(10, 600),
                )
            except requests.exceptions.ConnectionError:
                failed_urls.add(target.url)
                _log(f"[{_target_hostport(target)}] ConnectionError -> failover")
                continue
            except requests.exceptions.Timeout:
                failed_urls.add(target.url)
                _log(f"[{_target_hostport(target)}] Timeout -> failover")
                continue

            if ollama_resp.status_code != 200:
                err_body = ollama_resp.text
                failed_urls.add(target.url)
                _log(f"[{_target_hostport(target)}] HTTP {ollama_resp.status_code} -> failover")
                continue

            break

        # ── Response-Streaming / -Sammlung ────────────────────────

        def iter_ollama_chunks():
            for line in ollama_resp.iter_lines():
                if line:
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        _log(f"WARN: Ungueltiges JSON von Ollama: {line[:200]!r}")

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
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                            wrote_done = True
                        else:
                            data = f"data: {json.dumps(sse_event, ensure_ascii=False)}\n\n".encode("utf-8")
                            self.wfile.write(data)
                            self.wfile.flush()
                except BrokenPipeError:
                    _log(f"Client disconnected ({self.client_address[0]}:{self.client_address[1]})")
                    wrote_done = True
                except Exception:
                    _log(f"FEHLER im SSE-Stream: {traceback.format_exc()}")
                    if not wrote_done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                        wrote_done = True
                finally:
                    if not wrote_done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except Exception:
                            pass

                self.close_connection = True

            else:
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
            _log(f"[{_target_hostport(target)}] {'SSE' if streaming else 'JSON'} -> 200 ({duration:.2f}s)")

        except BrokenPipeError:
            raise
        except Exception:
            _log(f"FEHLER in Response-Translation: {traceback.format_exc()}")
            try:
                self._send_error(500, "Interner Fehler bei der Response-Uebersetzung")
            except Exception:
                pass

    def _send_error(self, code, msg):
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"error": msg}).encode())
        except BrokenPipeError:
            pass


# ── Server-Start ───────────────────────────────────────────────

def main():
    print(f"py-ollama-openai-bridge laeuft auf http://{HOST}:{PORT}", flush=True)
    print("Targets:", flush=True)
    for t in _all_targets:
        cfg = _build_effective_config(BRIDGE_CONFIG, t)
        ctx = cfg.get("num_ctx", "?")
        pred = cfg.get("num_predict", 32000)
        ka = cfg.get("keep_alive", "?")
        print(f"  {t.name}: {t.url}    num_ctx={ctx}    num_predict={pred}    keep_alive={ka}", flush=True)
    print(flush=True)

    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer gestoppt.")
        server.server_close()


if __name__ == "__main__":
    main()
