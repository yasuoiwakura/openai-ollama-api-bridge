#!/usr/bin/env python3
"""
HTTP Proxy: OpenAI /v1/chat/completions -> Ollama /api/chat

Empfängt OpenAI-kompatible Requests, uebersetzt sie mit translator.py
ins native Ollama-Format, leitet weiter, uebersetzt die Antwort zurueck
und streamt SSE an den Client.

Failover: OLLAMA_URL -> FAILOVER_OLLAMA_URL (bei ConnectionError/Timeout)

Queue-Mode: X-Bridge-Queue: on → serialisiert N parallele Requests auf 1,
             hält Verbindung per Keep-alive offen (kein OpenCode-Retry).

Features:
- Modellnamen-basierte Parameter: [key=value;key=value] im Modellnamen
- Erweiterte Queue-Modi: fifo, session, prio
- Timeout-Überwachung & Retry-Management
- SizeTracker: Lernt bei 413-Fehlern und verhindert zu große Requests
"""
import errno
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse

import requests

# ---- Load .env if present ----

def load_env(file_path: str = ".env"):
    if not os.path.isfile(file_path):
        return
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

load_env()

# ---- Utility for bool env ----

def get_bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "on", "yes")

from translator import translate_request, translate_response


class SizeTracker:
    """Lernt bei 413-Fehlern und verhindert zu große Requests.
    
    Speichert die kleinste dokumentierte Größe pro (api_url, api_key).
    Bei nächster Anfrage gleicher Kombination: Prüfe gegen gemerkte Größe.
    """
    
    def __init__(self):
        # {(api_url, api_key_hash): min_body_size}
        self.blacklist: Dict[Tuple[str, str], int] = {}
        self.lock = threading.RLock()
    
    def _make_key(self, api_url: str, api_key: str) -> Tuple[str, str]:
        """Erstellt einen eindeutigen Schlüssel für die Kombination."""
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return (api_url, api_key_hash)
    
    def record_413(self, api_url: str, api_key: str, body_size: int, error_type: str):
        """Dokumentiert den kleinsten Körper bei 413-Fehler."""
        key = self._make_key(api_url, api_key)
        
        with self.lock:
            if key not in self.blacklist:
                self.blacklist[key] = body_size
                _log(f"[size-tracker] NEW: {api_url[:50]}... min_size={body_size} "
                     f"error={error_type}")
            else:
                old_min = self.blacklist[key]
                if body_size < old_min:
                    self.blacklist[key] = body_size
                    _log(f"[size-tracker] UPDATED: {api_url[:50]}... "
                         f"min_size {old_min} -> {body_size} error={error_type}")
                else:
                    _log(f"[size-tracker] EXISTS: {api_url[:50]}... "
                         f"min_size={old_min} current={body_size} error={error_type}")
    
    def is_too_large(self, api_url: str, api_key: str, body_size: int) -> bool:
        """Prüft ob Request zu groß ist."""
        key = self._make_key(api_url, api_key)
        
        with self.lock:
            max_size = self.blacklist.get(key)
            if max_size and body_size > max_size:
                return True
            return False
    
    def get_min_size(self, api_url: str, api_key: str) -> Optional[int]:
        """Gibt die kleinste dokumentierte Größe zurück."""
        key = self._make_key(api_url, api_key)
        
        with self.lock:
            return self.blacklist.get(key)


def analyze_413_response(response_text: str) -> str:
    """Analysiert 413-Response.

    Liefert einen 'SIZE'-Typ NUR wenn die Fehlermeldung NACHWEISLICH
    eine Größen-/Context-Ursache nennt. Alles andere (Zeitfenster,
    parallele Requests, ...) ist "unknown_413" = KEINE Größen-Ursache.
    """
    text = (response_text or "").lower()

    if "context_length_exceeded" in text:
        return "context_length_exceeded"
    elif "too many tokens" in text or "token limit" in text:
        return "token_limit_exceeded"
    elif "maximum context length" in text or "exceeds the maximum context" in text:
        return "maximum_context_length"
    elif "request_too_large" in text:
        return "request_too_large"
    elif "payload too large" in text:
        return "payload_too_large"
    elif "request entity too large" in text:
        return "request_entity_too_large"
    elif "prompt is too long" in text or "reduce the length" in text:
        return "prompt_too_long"
    else:
        return "unknown_413"


# 413-Fehler, die NACHWEISLICH die Größe/den Context betreffen
SIZE_413_ERROR_TYPES = {
    "context_length_exceeded",
    "token_limit_exceeded",
    "maximum_context_length",
    "request_too_large",
    "payload_too_large",
    "request_entity_too_large",
    "prompt_too_long",
}


# Globale Instanz
_size_tracker = SizeTracker()


# ── RateLimitTracker: Block bei 429 mit Retry-After ─────────

class RateLimitTracker:
    def __init__(self):
        self._blocked: Dict[Tuple[str, str], Tuple[float, bytes]] = {}
        self.lock = threading.RLock()

    def block(self, api_url: str, api_key: str, body: bytes, retry_after: Optional[int] = None):
        # retry_after kommt aus dem Retry-After Header, sonst 60s Fallback
        duration = int(retry_after) if retry_after is not None and retry_after > 0 else 60
        # clamp: 1s .. 600s
        duration = max(1, min(duration, 600))
        key = (api_url, hashlib.sha256(api_key.encode()).hexdigest()[:16])
        with self.lock:
            self._blocked[key] = (time.time() + duration, body)
            _log(f"### [429 RATE-LIMIT] BLOCKED {api_url[:50]}... für {duration}s (Retry-After={retry_after}) ###")
            _log(f"### [429 RATE-LIMIT] Weitere Requests an dieses Target werden {duration}s mit 429 + Retry-After beantwortet ###")

    def get_error(self, api_url: str, api_key: str) -> Optional[bytes]:
        key = (api_url, hashlib.sha256(api_key.encode()).hexdigest()[:16])
        with self.lock:
            entry = self._blocked.get(key)
            if entry is None:
                return None
            until, body = entry
            if time.time() >= until:
                del self._blocked[key]
                return None
            return body

    def get_retry_after(self, api_url: str, api_key: str) -> Optional[int]:
        """Verbleibende Block-Sekunden (aufgerundet) oder None."""
        key = (api_url, hashlib.sha256(api_key.encode()).hexdigest()[:16])
        with self.lock:
            entry = self._blocked.get(key)
            if entry is None:
                return None
            until, _ = entry
            remaining = until - time.time()
            if remaining <= 0:
                del self._blocked[key]
                return None
            return int(remaining + 0.999)  # ceil


_rate_limit_tracker = RateLimitTracker()


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Parst Retry-After Header (Sekunden oder HTTP-Date). Liefert Sekunden oder None."""
    if not value:
        return None
    v = value.strip()
    # Sekunden als Integer
    try:
        secs = int(v)
        if secs >= 0:
            return secs
    except ValueError:
        pass
    # HTTP-Date fallback (Retry-After als Datum)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        if dt is not None:
            # aware datetime -> timestamp
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            delta = (dt - now).total_seconds()
            if delta > 0:
                return int(delta + 0.999)
    except Exception:
        pass
    return None


def _parse_model_name(model_name: str) -> tuple[str, dict]:
    """Extrahiert Parameter aus Modellnamen.
    
    Input:  "qwen3.5:9b[num_ctx=32768;temperature=0.7]"
    Output: ("qwen3.5:9b", {"num_ctx": "32768", "temperature": "0.7"})
    """
    if not model_name:
        return model_name, {}
    
    pattern = r'^(.*?)\[(.*?)\]$'
    match = re.match(pattern, model_name)
    if not match:
        return model_name, {}
    
    base_name = match.group(1)
    params_str = match.group(2)
    
    params = {}
    if params_str:
        for param in params_str.split(';'):
            if '=' in param:
                key, value = param.split('=', 1)
                params[key.strip()] = value.strip()
    
    return base_name, params


if __name__ == "__main__":
    # Einfacher Test für die Kommandozeile
    import sys
    test_cases = [
        "qwen3.5:9b",
        "qwen3.5:9b[Prio=1]",
        "qwen3.5:9b[Prio=1;session-id=abc123]",
        "deepseek-v4-flash[num_ctx=32768;temperature=0.7;Prio=5]",
        "model[]",
        "openai/gpt-4o[Prio=1]",
    ]
    
    for test in test_cases:
        name, params = _parse_model_name(test)
        print(f"Input:  {test}")
        print(f"Output: ({name}, {params})")
        print()


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


class UpstreamStallWatcher:
    """Watches upstream + misst TPS. Unterscheidet STALL (0 TPS) vs DEGRADED (wenig TPS).

    - STALL: keine Daten seit 5/10/20s =>  !!! [STALL]
    - DEGRADED: Daten fliessen aber < low_tps_threshold =>  ~~~ [SLOW]
    Touch()/record() muss pro empfangenem Chunk/SSE-Event aufgerufen werden.
    """

    def __init__(self, entry_id: str, label: str = "UPSTREAM", thresholds=(5, 10, 20),
                 low_tps_threshold: float = 1.0, degraded_interval: float = 15.0):
        self.entry_id = entry_id
        self.label = label
        self.thresholds = tuple(thresholds)
        self.low_tps_threshold = float(low_tps_threshold)
        self.degraded_interval = float(degraded_interval)
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._logged = set()
        self._thread: Optional[threading.Thread] = None
        # TPS-Tracking
        self._events: deque[float] = deque()
        self._lock = threading.Lock()
        self.total_events: int = 0
        self.total_bytes: int = 0
        self._last_degraded_log: float = 0.0
        self._started_at = time.monotonic()

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"stall-{self.entry_id}"
        )
        self._thread.start()

    def touch(self):
        self._last = time.monotonic()
        self._logged.clear()

    def record(self, nbytes: int = 0):
        """Ein Chunk/Event eingetroffen – für TPS zählen und Stall zurücksetzen."""
        now = time.monotonic()
        with self._lock:
            self._events.append(now)
            self.total_events += 1
            self.total_bytes += int(nbytes)
            # prune >30s
            while self._events and self._events[0] < now - 30:
                self._events.popleft()
        self.touch()

    def get_tps(self, window: float = 10.0) -> float:
        now = time.monotonic()
        with self._lock:
            # prune
            while self._events and self._events[0] < now - window:
                # keep up to 30s, but for window calc only count within window
                # we already pruned 30s above, so just count
                if self._events[0] < now - window:
                    # don't popleft 30s deque here, just count
                    pass
                break
            cnt = sum(1 for t in self._events if t >= now - window)
            return cnt / window if window > 0 else 0.0

    def get_stats(self, window: float = 10.0) -> tuple[float, int]:
        """(tps, count_in_window)"""
        now = time.monotonic()
        with self._lock:
            cnt = sum(1 for t in self._events if t >= now - window)
            tps = cnt / window if window > 0 else 0.0
            return tps, cnt

    def avg_tps_total(self) -> float:
        elapsed = time.monotonic() - self._started_at
        if elapsed <= 0:
            return 0.0
        return self.total_events / elapsed

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _run(self):
        # prüft jede 0.5s
        while not self._stop.wait(0.5):
            elapsed = time.monotonic() - self._last
            # 1) echter STALL (keine Daten)
            stalled = False
            for thr in self.thresholds:
                if elapsed >= thr and thr not in self._logged:
                    self._logged.add(thr)
                    stalled = True
                    _log(f"!!! [STALL] entry={self.entry_id} {self.label} keine Antwort seit {thr}s (elapsed {elapsed:.1f}s) !!!")
            if stalled:
                continue
            # 2) DEGRADED: Daten kommen, aber sehr langsam (wenig TPS) – kein Stall, aber gedrosselt
            # nur wenn nicht stalled (elapsed <5s) und genug Laufzeit
            if elapsed < 5.0:
                tps, cnt = self.get_stats(window=10.0)
                total_elapsed = time.monotonic() - self._started_at
                # erst nach 10s Laufzeit und mindestens 3 Events bewerten
                if total_elapsed >= 10.0 and self.total_events >= 3:
                    if 0 < tps < self.low_tps_threshold:
                        now = time.monotonic()
                        if now - self._last_degraded_log >= self.degraded_interval:
                            self._last_degraded_log = now
                            avg = self.avg_tps_total()
                            _log(f"~~~ [SLOW] entry={self.entry_id} {self.label} wenig Durchsatz {tps:.2f} TPS (10s: {cnt} events, avg {avg:.2f} TPS, total {self.total_events} events) - gedrosselt, halte Verbindung offen ~~~")

    def _next_thr(self, current: int) -> str:
        idx = self.thresholds.index(current) if current in self.thresholds else -1
        if idx + 1 < len(self.thresholds):
            return str(self.thresholds[idx + 1])
        return "—"


def _target_hostport(target: Optional['Target']) -> str:
    if target is None:
        return "?"
    p = urlparse(target.url)
    host = p.hostname or target.url
    port = f":{p.port}" if p.port else ""
    return f"{host}{port}"


def _ollama_pull_model(ollama_url: str, model: str) -> Tuple[bool, Optional[str]]:
    """Pullt ein Modell von Ollama. Blocking mit Timeout.
    
    Returns:
        (success, error_message) - success=True bei Erfolg
    """
    try:
        resp = requests.post(
            f"{ollama_url}/api/pull",
            json={"model": model},
            stream=True,
            timeout=(10, 600)
        )
        for line in resp.iter_lines():
            if line:
                try:
                    status = json.loads(line)
                    if status.get("status") == "success":
                        return True, None
                    if "error" in status:
                        return False, status["error"]
                except json.JSONDecodeError:
                    pass
        return False, "Pull interrupted"
    except requests.exceptions.ConnectionError:
        return False, "Connection error during pull"
    except Exception as e:
        return False, str(e)


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


def _resolve_target(server_param: str) -> Optional[Target]:
    """Resolve server parameter from model name to a Target.
    
    Supports:
    - "ollama" or "1" -> Primary server
    - "failover" or "2" -> Secondary server
    - Numeric values > 2 -> Index in targets list
    """
    if not server_param:
        return None
    
    param = server_param.strip().lower()
    
    # Named targets
    if param in ("ollama", "1"):
        idx = 0
    elif param in ("failover", "2"):
        idx = 1
    else:
        # Try numeric index
        try:
            idx = int(param) - 1
        except ValueError:
            return None
    
    if 0 <= idx < len(_all_targets):
        return _all_targets[idx]
    return None


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

# Client darf die .env-Overrides pro Request abschalten?
# True (Default): Header "X-Bridge-Override: off" wird beachtet → Modelfile-Einstellungen greifen.
# False: Header wird ignoriert (dem Client wird das Recht entzogen).
HEADER_OVERRULES = get_bool_env("HEADER_OVERRULES", True)

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


# ── Queue-Konfiguration ─────────────────────────────────────────

QUEUE_ENABLED = get_bool_env("QUEUE_ENABLED", False)
QUEUE_TARGET_URL = os.getenv("QUEUE_TARGET_URL", "").rstrip("/")  # optional: default-upstream
QUEUE_TARGET_KEY = os.getenv("QUEUE_TARGET_KEY", "")  # optional: statischer key
QUEUE_MODE_DEFAULT = os.getenv("QUEUE_MODE_DEFAULT", "fifo").strip().lower()
QUEUE_KEEPALIVE = float(os.getenv("QUEUE_KEEPALIVE", "15"))
QUEUE_CONNECT_TIMEOUT = float(os.getenv("QUEUE_CONNECT_TIMEOUT", "60"))
HIDE_HEALTHCHECK = get_bool_env("HIDE_HEALTHCHECK", False)
QUEUE_STREAM_TIMEOUT = float(os.getenv("QUEUE_STREAM_TIMEOUT", "600"))
QUEUE_MAX_SIZE = int(os.getenv("QUEUE_MAX_SIZE", "500"))
QUEUE_MAX_WAITTIME = int(os.getenv("QUEUE_MAX_WAITTIME", "300"))
QUEUE_MAX_RETRIES = int(os.getenv("QUEUE_MAX_RETRIES", "5"))
QUEUE_SESSION_TRACKING = get_bool_env("QUEUE_SESSION_TRACKING", True)
# Wartezeit BEVOR ein 200 OK an eine wartende Anfrage gesendet wird (0 = sofort)
QUEUE_FALLBACK_TIMEOUT = float(os.getenv("QUEUE_FALLBACK_TIMEOUT", "15"))
# Pause zwischen Ende eines Upstream-Requests und Start des nächsten (Sekunden)
# Verhindert Upstream-Timeouts/Throttling bei burst-artigen Calls.
# Fester Default via compose.yml, override via .env UPSTREAM_PAUSE.
# Liest UPSTREAM_PAUSE primär, fallback QUEUE_GAP / QUEUE_UPSTREAM_GAP für Kompatibilität.
_upstream_pause_raw = os.getenv(
    "UPSTREAM_PAUSE",
    os.getenv("QUEUE_GAP", os.getenv("QUEUE_UPSTREAM_GAP", "1.5")),
)
try:
    UPSTREAM_PAUSE = float(_upstream_pause_raw)
except (ValueError, TypeError):
    UPSTREAM_PAUSE = 1.5
if UPSTREAM_PAUSE < 0:
    UPSTREAM_PAUSE = 0.0
# Rückwärtskompatibilität: alter Name als Alias
QUEUE_GAP = UPSTREAM_PAUSE
# Schwelle für DEGRADED-Erkennung (TPS = SSE-Events/s). Unter diesem Wert gilt Upstream als gedrosselt.
try:
    UPSTREAM_LOW_TPS = float(os.getenv("UPSTREAM_LOW_TPS", "1.0"))
except (ValueError, TypeError):
    UPSTREAM_LOW_TPS = 1.0
if UPSTREAM_LOW_TPS < 0:
    UPSTREAM_LOW_TPS = 0.0
# 413-Learning (SizeTracker): global an/aus (per X-Bridge-Size-Tracker ueberschreibbar)
BRIDGE_SIZE_TRACKER = get_bool_env("BRIDGE_SIZE_TRACKER", True)
# Maximale Groesse fuer transparentere Fehler-Responses (echter Upstream-Body)
MAX_ERROR_BODY = int(os.getenv("MAX_ERROR_BODY", "65536"))



# ── Queue: Entry / Scheduler / Worker ───────────────────────────

class _QueueEntry:
    __slots__ = ("id", "group", "stream", "payload", "result", "cancel",
                 "target_url", "target_key", "enqueued_at", "group_open_at",
                 "prio", "session_id", "retry_count", "model_name",
                 "original_payload_size", "http_status", "size_tracker_enabled",
                 "http_content_type", "http_error_body",
                 "upstream_start", "upstream_duration",
                 "upstream_events", "upstream_bytes", "upstream_avg_tps")

    def __init__(self, req_id, group, stream, payload, target_url, target_key,
                 prio=100, session_id=None, retry_count=0, model_name=None,
                 original_payload_size=None, http_status=200,
                 size_tracker_enabled=True):
        self.id = req_id
        self.group = group
        self.stream = stream
        self.payload = payload
        self.result = queue.Queue(maxsize=64)
        self.cancel = threading.Event()
        self.target_url = target_url
        self.target_key = target_key
        self.enqueued_at = time.time()
        self.group_open_at = time.time()
        self.prio = prio
        self.session_id = session_id
        self.retry_count = retry_count
        self.model_name = model_name
        self.original_payload_size = original_payload_size or len(payload)
        # Echter HTTP-Status der Upstream-Antwort (wird vom Worker gesetzt)
        self.http_status = http_status
        # 413-Learning per Request an/aus (env/header)
        self.size_tracker_enabled = size_tracker_enabled
        # Transparentes Weiterreichen von Fehler-Responses (Worker setzt beides)
        self.http_content_type = "application/json"
        self.http_error_body = None
        # Timing: Upstream-Dauer (vom Worker gesetzt)
        self.upstream_start: Optional[float] = None
        self.upstream_duration: Optional[float] = None
        self.upstream_events: int = 0
        self.upstream_bytes: int = 0
        self.upstream_avg_tps: Optional[float] = None


def _prompt_signature(payload: dict) -> tuple[str, str]:
    """(primary_exact, topic_hint)."""
    messages = payload.get("messages", [])

    def _text_from(role):
        for m in messages:
            if m.get("role") == role:
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(p.get("text", "") for p in c
                                    if isinstance(p, dict) and p.get("type") == "text")
        return ""

    primary = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    topic_hint = hashlib.sha256(
        (_text_from("system")[:2000] + "|" + _text_from("user")[:2000]).encode()
    ).hexdigest()
    return primary, topic_hint


def _queue_group(payload: dict) -> str:
    primary, topic = _prompt_signature(payload)
    return f"t:{topic}"


class _QueueScheduler:
    """Erweiterter Scheduler mit fifo, session und prio Modi."""

    def __init__(self, mode: str):
        self.mode = mode
        self.lock = threading.RLock()
        self._entries: deque[_QueueEntry] = deque()
        self.total = 0
        self.session_counters = {}  # Retry-Counter pro Session

    def submit(self, entry: _QueueEntry) -> bool:
        with self.lock:
            if self.total >= QUEUE_MAX_SIZE:
                return False
            self._entries.append(entry)
            self.total += 1
            return True

    def remove(self, entry: _QueueEntry):
        with self.lock:
            try:
                self._entries.remove(entry)
                self.total -= 1
            except ValueError:
                pass

    def next_entry(self) -> Optional[_QueueEntry]:
        with self.lock:
            if self.mode == "fifo":
                return self._next_fifo()
            elif self.mode == "session":
                return self._next_by_session()
            elif self.mode == "prio":
                return self._next_by_priority()
            else:
                return self._next_fifo()

    def _next_fifo(self) -> Optional[_QueueEntry]:
        """FIFO: Erst kommt, zuerst wird bedient."""
        if not self._entries:
            return None
        entry = self._entries.popleft()
        self.total -= 1
        return entry

    def _next_by_session(self) -> Optional[_QueueEntry]:
        """Session: Bevorzugt Anfragen der gleichen Session."""
        if not self._entries:
            return None
        
        # Finde die Session mit den meisten wartenden Anfragen
        session_counts = {}
        for entry in self._entries:
            session_id = entry.session_id or "default"
            session_counts[session_id] = session_counts.get(session_id, 0) + 1
        
        if not session_counts:
            return self._next_fifo()
        
        # Bevorzuge Session mit den meisten wartenden Anfragen
        preferred_session = max(session_counts.items(), key=lambda x: x[1])[0]
        
        # Finde älteste Anfrage dieser Session
        for i, entry in enumerate(self._entries):
            session_id = entry.session_id or "default"
            if session_id == preferred_session:
                self._entries.remove(entry)
                self.total -= 1
                return entry
        
        return self._next_fifo()

    def _next_by_priority(self) -> Optional[_QueueEntry]:
        """Prio: Bevorzugt höchste Priorität (niedrigste Zahl)."""
        if not self._entries:
            return None
        
        # Sortiere nach Priorität (aufsteigend), dann nach enqueued_at
        sorted_entries = sorted(self._entries, key=lambda e: (e.prio, e.enqueued_at))
        entry = sorted_entries[0]
        
        # Entferne aus der Queue
        try:
            self._entries.remove(entry)
            self.total -= 1
        except ValueError:
            pass
        
        return entry

    def increment_retry_count(self, session_id: str) -> int:
        """Erhöht den Retry-Counter für eine Session."""
        with self.lock:
            if session_id not in self.session_counters:
                self.session_counters[session_id] = 0
            self.session_counters[session_id] += 1
            return self.session_counters[session_id]

    def reset_retry_count(self, session_id: str):
        """Setzt den Retry-Counter für eine Session zurück."""
        with self.lock:
            if session_id in self.session_counters:
                self.session_counters[session_id] = 0


def _iter_sse_events(resp):
    """Yields raw SSE event bytes from a streaming response."""
    buf = b""
    try:
        for line in resp.iter_lines(decode_unicode=False):
            if line in (b"", b"\r", b"\r\n"):
                if buf:
                    yield buf + b"\n\n"
                    buf = b""
            else:
                buf += line + b"\n"
    finally:
        if buf:
            yield buf + b"\n\n"


class _QueueWorker(threading.Thread):
    def __init__(self, scheduler: _QueueScheduler):
        super().__init__(daemon=True, name="queue-worker")
        self.scheduler = scheduler
        self.running = True
        self.max_wait_time = QUEUE_MAX_WAITTIME
        self._last_finished_at: float = 0.0  # monotonic timestamp des letzten Request-Endes

    def run(self):
        while self.running:
            # Regelmäßige Prüfung auf wartende Anfragen
            self._check_timeouts()

            # Pause zwischen Ende des letzten und Start des nächsten Upstream-Calls
            if UPSTREAM_PAUSE > 0 and self._last_finished_at > 0:
                elapsed = time.monotonic() - self._last_finished_at
                remaining = UPSTREAM_PAUSE - elapsed
                if remaining > 0:
                    # Nur warten wenn tatsächlich etwas in der Queue wartet
                    with self.scheduler.lock:
                        has_pending = self.scheduler.total > 0 or len(self.scheduler._entries) > 0
                    if has_pending:
                        _log(f"[queue] pause {elapsed:.2f}/{UPSTREAM_PAUSE:.2f}s UPSTREAM_PAUSE - wait {remaining:.2f}s")
                        time.sleep(remaining)
                    else:
                        # Keine pending Entries -> Gap beim nächsten Durchlauf erneut prüfen
                        pass

            try:
                entry = self.scheduler.next_entry()
            except Exception:
                entry = None
            if entry is None:
                time.sleep(0.05)
                continue
            try:
                self._process(entry)
            finally:
                self._last_finished_at = time.monotonic()

    def _check_timeouts(self):
        """Prüft auf wartende Anfragen nahe dem Timeout."""
        current_time = time.time()
        with self.scheduler.lock:
            for entry in list(self.scheduler._entries):
                wait_time = current_time - entry.enqueued_at
                
                # Bei 90% des Timeouts: Anfrage durchlassen (auch wenn andere höhere Prio haben)
                # Dies verhindert OpenCode-Timeouts
                if wait_time > self.max_wait_time * 0.9:
                    # Erhöhe Retry-Counter
                    session_id = entry.session_id or "default"
                    retry_count = self.scheduler.increment_retry_count(session_id)
                    
                    if retry_count >= QUEUE_MAX_RETRIES:
                        # Max Retries erreicht - entferne aus Queue
                        self.scheduler.remove(entry)
                        entry.result.put(json.dumps({
                            "error": f"Max retries ({QUEUE_MAX_RETRIES}) erreicht"
                        }).encode())
                        entry.result.put(None)
                        _log(f"[queue] entry={entry.id} removed: max retries reached")
                    else:
                        # Erhöhe Priorität dynamisch auf 1 (höchste)
                        entry.prio = 1
                        entry.retry_count = retry_count
                        _log(f"[queue] entry={entry.id} timeout warning: retry={retry_count}, new_prio={entry.prio}")

    def _process(self, entry: _QueueEntry):
        _log(f"[queue] entry={entry.id} group={entry.group[:12]} start")
        if entry.cancel.is_set():
            return

        # Fast-fail wenn Upstream gerade wegen 429 geblockt ist (verhindert Hammering)
        if entry.target_key:
            cached = _rate_limit_tracker.get_error(entry.target_url, entry.target_key)
            if cached is not None:
                retry_after = _rate_limit_tracker.get_retry_after(entry.target_url, entry.target_key) or 60
                entry.http_status = 429
                entry.http_content_type = "application/json"
                entry.http_error_body = cached
                if not entry.cancel.is_set():
                    try:
                        entry.result.put(cached, timeout=2)
                        entry.result.put(None)
                    except queue.Full:
                        _log(f"[queue] entry={entry.id} result queue full on rate-limit fast-fail, dropping")
                _log(f"### [429 FAST-FAIL] entry={entry.id} geblockt — noch {retry_after}s bis Retry-After abläuft ###")
                _log(f"### [429 FAST-FAIL] Kein Upstream-Call, gecachten 429 direkt zurückgegeben ###")
                return

        input_size = len(entry.payload)
        original_size = entry.original_payload_size  # Ursprüngliche Größe von OpenCode
        
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(input_size),
            "Accept": "text/event-stream" if entry.stream else "application/json",
        }
        if entry.target_key:
            headers["Authorization"] = f"Bearer {entry.target_key}"
            headers["x-api-key"] = entry.target_key
        chat_url = f"{entry.target_url}/chat/completions"
        # Timing: UPSTREAM + TPS
        entry.upstream_start = time.monotonic()
        entry.upstream_duration = None
        entry.upstream_events = 0
        entry.upstream_bytes = 0
        entry.upstream_avg_tps = None
        watcher = UpstreamStallWatcher(entry.id, label=f"UPSTREAM {entry.target_url[:40]}",
                                       low_tps_threshold=UPSTREAM_LOW_TPS)
        watcher.start()
        resp = None
        try:
            watcher.record(0)
            resp = requests.post(
                chat_url, data=entry.payload, headers=headers,
                stream=True, timeout=(QUEUE_CONNECT_TIMEOUT, QUEUE_STREAM_TIMEOUT),
            )
            watcher.record(0)
            entry.http_status = resp.status_code  # echter Upstream-Status
            if resp.status_code != 200:
                # Echten Upstream-Body sichern (für transparentes 1:1-Durchreichen)
                body_bytes = b""
                try:
                    body_bytes = resp.raw.read(MAX_ERROR_BODY + 1)
                    if len(body_bytes) > MAX_ERROR_BODY:
                        body_bytes = body_bytes[:MAX_ERROR_BODY]
                    # Gzip/deflate decodieren falls Upstream komprimiert
                    ce = resp.headers.get("Content-Encoding", "").lower()
                    if ce == "gzip":
                        import gzip
                        body_bytes = gzip.decompress(body_bytes)
                    elif ce == "deflate":
                        import zlib
                        body_bytes = zlib.decompress(body_bytes, -zlib.MAX_WBITS)
                except Exception:
                    body_bytes = b""
                body_text = body_bytes.decode("utf-8", "replace")
                entry.http_content_type = (
                    resp.headers.get("Content-Type") or "text/plain"
                )
                entry.http_error_body = body_bytes
                err = f"HTTP {resp.status_code}: {body_text[:200]}"
                
                # ── 413 speziell behandeln ─────────────────────────────────
                if resp.status_code == 413:
                    output_size = input_size
                    error_type = analyze_413_response(body_text)
                    is_size_error = error_type in SIZE_413_ERROR_TYPES
                    
                    if is_size_error and entry.size_tracker_enabled:
                        # NUR nachgewiesene Größen-Fehler in die Blacklist lernen
                        _size_tracker.record_413(entry.target_url, entry.target_key,
                                                input_size, error_type)
                        _log(f"[queue] entry={entry.id} 413 (SIZE) type={error_type} "
                             f"Original={original_size} Input={input_size} Output={output_size}")
                        if error_type in ("context_length_exceeded", "maximum_context_length",
                                          "token_limit_exceeded"):
                            _log(f"[queue] entry={entry.id} → Context-Fehler erkannt → "
                                 f"echter 413 an OpenCode → Compaction")
                    else:
                        # KEIN Größen-Fehler (Zeitfenster, parallele Anfragen, ...)
                        # oder 413-Learning deaktiviert
                        # → NICHT lernen, Fehler 1:1 an Client durchreichen
                        reason = "tracker-off" if not entry.size_tracker_enabled else "kein Größen-Fehler"
                        _log(f"[queue] entry={entry.id} 413 (OTHER) type={error_type} "
                             f"({reason}) Original={original_size} Input={input_size} Output={output_size}")
                        _log(f"[queue] entry={entry.id} → NICHT in Blacklist gelernt, "
                             f"Fehler 1:1 durchgereicht")
                    
                    if not entry.cancel.is_set():
                        try:
                            entry.result.put(json.dumps({"error": err}).encode(), timeout=2)
                            entry.result.put(None)
                        except queue.Full:
                            _log(f"[queue] entry={entry.id} result queue full on 413, dropping")
                    resp.close()
                    _log(f"[queue] entry={entry.id} upstream error: {err}")
                    return
                
                # ── 429 → Retry-After Header auswerten + Block ───────────────
                if resp.status_code == 429 and entry.target_key:
                    retry_after_hdr = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                    retry_after = _parse_retry_after(retry_after_hdr)
                    # Fallback: window-opens Texte immer blocken, sonst generisch 429 blocken
                    is_window = "window opens" in body_text.lower() or "fair use" in body_text.lower()
                    if retry_after is not None or is_window or resp.status_code == 429:
                        # Wenn kein Header aber is_window -> 60s, sonst Header-Wert oder 60s Fallback
                        _rate_limit_tracker.block(
                            entry.target_url, entry.target_key, body_bytes, retry_after=retry_after)
                        _log(f"### [429 THROTTLE] entry={entry.id} UPSTREAM 429 - Retry-After={retry_after_hdr!r} -> block {retry_after or 60}s ###")
                        _log(f"### [429 THROTTLE] Body: {body_text[:180]} ###")
                
                if not entry.cancel.is_set():
                    try:
                        entry.result.put(json.dumps({"error": err}).encode(), timeout=2)
                        entry.result.put(None)
                    except queue.Full:
                        _log(f"[queue] entry={entry.id} result queue full on error, dropping")
                try:
                    resp.close()
                except Exception:
                    pass
                # UPSTREAM timing für Fehler
                if entry.upstream_start is not None:
                    entry.upstream_duration = time.monotonic() - entry.upstream_start
                    _log(f"[queue] entry={entry.id} UPSTREAM error {entry.http_status} in {entry.upstream_duration:.2f}s")
                _log(f"[queue] entry={entry.id} upstream error: {err}")
                return
            # --- Streaming: TPS + stall-watched (transparente Weiterleitung) ---
            try:
                for ev in _iter_sse_events(resp):
                    watcher.record(len(ev))
                    if entry.cancel.is_set():
                        _log(f"[queue] entry={entry.id} cancel detected during streaming, aborting")
                        break
                    try:
                        entry.result.put(ev, timeout=2)
                    except queue.Full:
                        if entry.cancel.is_set():
                            _log(f"[queue] entry={entry.id} cancel + queue full, aborting")
                            break
                        # Queue trotzdem voll → 2s nochmal versuchen, dann aufgeben
                        _log(f"[queue] entry={entry.id} result queue full, waiting")
                        entry.result.put(ev, timeout=5)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if not entry.cancel.is_set():
                entry.result.put(None)
            if entry.upstream_start is not None:
                entry.upstream_duration = time.monotonic() - entry.upstream_start
                # TPS final
                try:
                    entry.upstream_events = watcher.total_events
                    entry.upstream_bytes = watcher.total_bytes
                    entry.upstream_avg_tps = watcher.avg_tps_total()
                    tps_str = f"{entry.upstream_avg_tps:.2f} TPS" if entry.upstream_avg_tps is not None else "n/a"
                    _log(f"[queue] entry={entry.id} UPSTREAM done in {entry.upstream_duration:.2f}s (status {entry.http_status}, {entry.upstream_events} events, {tps_str})")
                    # bei sehr niedriger TPS zusätzlich downgraded-Hinweis (bereits via Watcher geloggt, hier final)
                    if entry.upstream_events > 3 and entry.upstream_avg_tps is not None and entry.upstream_avg_tps < UPSTREAM_LOW_TPS:
                        _log(f"~~~ [SLOW] entry={entry.id} final avg {tps_str} über {entry.upstream_duration:.1f}s - Upstream gedrosselt, nicht abgebrochen ~~~")
                except Exception:
                    _log(f"[queue] entry={entry.id} UPSTREAM done in {entry.upstream_duration:.2f}s (status {entry.http_status})")
            else:
                _log(f"[queue] entry={entry.id} upstream done")
        except Exception as e:
            # UPSTREAM timing auch bei Exception
            if entry.upstream_start is not None and entry.upstream_duration is None:
                try:
                    entry.upstream_duration = time.monotonic() - entry.upstream_start
                    entry.upstream_events = watcher.total_events if 'watcher' in locals() else 0
                    entry.upstream_avg_tps = watcher.avg_tps_total() if 'watcher' in locals() else None
                except Exception:
                    entry.upstream_duration = time.monotonic() - entry.upstream_start
                tps_s = f"{entry.upstream_avg_tps:.2f} TPS" if isinstance(getattr(entry,'upstream_avg_tps',None),(int,float)) else "n/a"
                _log(f"[queue] entry={entry.id} UPSTREAM exception after {entry.upstream_duration:.2f}s ({tps_s}): {e}")
            entry.http_status = 502  # Bridge konnte nicht via Queue verbinden
            entry.http_content_type = "application/json"
            entry.http_error_body = str(e).encode("utf-8", "replace")
            if not entry.cancel.is_set():
                try:
                    entry.result.put(json.dumps({"error": str(e)}).encode(), timeout=2)
                    entry.result.put(None)
                except queue.Full:
                    _log(f"[queue] entry={entry.id} result queue full on error, dropping")
            _log(f"[queue] entry={entry.id} exception: {e}")
        finally:
            try:
                watcher.stop()
            except Exception:
                pass


# ── Globals für Queue ───────────────────────────────────────────

_queue_scheduler: Optional[_QueueScheduler] = None
_queue_worker: Optional[_QueueWorker] = None

# Sentinel: Ergibt sich beim Wait auf entry.result NUR bei Fenster-Ablauf
_NO_ITEM = object()


def _ensure_queue():
    global _queue_scheduler, _queue_worker
    if _queue_scheduler is not None:
        return
    _queue_scheduler = _QueueScheduler(QUEUE_MODE_DEFAULT)
    _queue_worker = _QueueWorker(_queue_scheduler)
    _queue_worker.start()


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
        except Exception:
            _log(f"Exception in request handling: {traceback.format_exc()}")
            self.send_error(500, "Internal Server Error")
        return True

    def log_request(self, code='-', size='-'):
        global HIDE_HEALTHCHECK
        # Skip logging for health check if disabled
        if self.path == "/health" and HIDE_HEALTHCHECK:
            return
        target = getattr(self, '_current_target', None)
        hp = _target_hostport(target)
        _log(f"[{hp}] {self.command} {self.path} -> {code}")

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, "
                         "X-Bridge-Override, X-Bridge-Translate, X-Bridge-Queue, "
                         "X-Bridge-Queue-Mode, X-Bridge-Target-URL, X-Bridge-Target-Key, "
                         "X-Bridge-Prio, X-Bridge-Max-Waittime, "
                         "X-Bridge-Size-Tracker, X-Bridge-Fallback-Timeout")

    @staticmethod
    def _extract_error(data: bytes) -> Optional[str]:
        """Liefert den Fehlertext, wenn data ein JSON-Objekt {"error": ...} ist."""
        try:
            obj = json.loads(data.decode("utf-8"))
            if isinstance(obj, dict) and "error" in obj:
                e = obj.get("error")
                if isinstance(e, str):
                    return e
                return json.dumps(e)
        except (ValueError, json.JSONDecodeError):
            pass
        return None

    def _send_error(self, code, msg):
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"error": msg}).encode())
        except BrokenPipeError:
            pass

    def do_OPTIONS(self):
        _log(f"[?] incoming: {self.command} {self.path}")
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        if not (HIDE_HEALTHCHECK and self.path == "/health"):
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

        # ── Header-Modi ─────────────────────────────────────────
        translate = self.headers.get("X-Bridge-Translate", "").strip().lower()
        do_translate = translate != "off"

        queue_on = self.headers.get("X-Bridge-Queue", "").strip().lower()
        # Queue aktiv wenn: Header explizit "on" ODER .env "QUEUE_ENABLED=1"
        queue_enabled = queue_on in ("on", "true", "1") or QUEUE_ENABLED
        
        # Queue-Modus: fifo, session, prio
        queue_mode = self.headers.get("X-Bridge-Queue-Mode", "").strip().lower() or QUEUE_MODE_DEFAULT
        if queue_on in ("on", "true", "1"):
            queue_mode = "fifo"  # Kompatibilität zu "on"
        
        target_url = self.headers.get("X-Bridge-Target-URL", "").strip() or QUEUE_TARGET_URL
        target_key = self.headers.get("X-Bridge-Target-Key", "").strip() or QUEUE_TARGET_KEY
        
        # ── 413-Learning (SizeTracker) per Request ───────────────
        size_tracker_enabled = BRIDGE_SIZE_TRACKER
        size_tracker_header = self.headers.get("X-Bridge-Size-Tracker", "").strip().lower()
        if size_tracker_header:
            size_tracker_enabled = size_tracker_header not in ("0", "false", "off", "no")
        
        # ── Fallback-Timeout (Wartezeit vor dem 200er) per Request ─
        fallback_timeout = QUEUE_FALLBACK_TIMEOUT
        fallback_header = self.headers.get("X-Bridge-Fallback-Timeout", "").strip()
        if fallback_header:
            try:
                fallback_timeout = max(0.0, float(fallback_header))
            except ValueError:
                pass
        
        # ── Modellnamen-basierte Parameter ──────────────────────
        model_name = None
        model_params = {}
        try:
            openai_req_preview = json.loads(raw_body)
            model_name = openai_req_preview.get("model", "")
            if model_name:
                clean_name, params = _parse_model_name(model_name)
                model_params = params
                # Bereinigter Modellname wird später für Ollama verwendet
        except:
            pass

        # ── Session-ID extrahieren ──────────────────────────────
        session_id = model_params.get("session-id") or model_params.get("session_id")
        
        # ── Priorität extrahieren ───────────────────────────────
        prio = 100  # Default: mittlere Priorität
        if "Prio" in model_params:
            try:
                prio = int(model_params["Prio"])
            except ValueError:
                pass

        # ── Queue-Modus: Original Body 1:1 weiterleiten ─────────
        if queue_enabled:
            self._handle_queue(raw_body, target_url, target_key, queue_mode,
                              session_id=session_id, prio=prio,
                              size_tracker_enabled=size_tracker_enabled,
                              fallback_timeout=fallback_timeout)
            return

        # ── Normaler Modus: JSON parsen ─────────────────────────
        try:
            openai_req = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_error(400, "Ungueltiges JSON")
            return
        
        # Modellname bereinigen für Ollama
        if model_name:
            clean_name, model_params = _parse_model_name(model_name)
            openai_req["model"] = clean_name

        # ── Normaler Modus: Ollama-Translation ──────────────────
        streaming = openai_req.get("stream", False)
        
        # Check for forced target via model parameter [server=...]
        forced_target = None
        if "server" in model_params:
            forced_target = _resolve_target(model_params["server"])
            if forced_target is None:
                self._send_error(400, f"Ungueltiger server-Parameter: {model_params['server']}")
                return
            _log(f"[server-override] Forced target: {forced_target.name} ({forced_target.url})")

        def _send_request_to_target(target: Target):
            """Send request to specific target and handle response."""
            self._current_target = target
            effective_config = _build_effective_config(BRIDGE_CONFIG, target)
            
            override_mode = "on"
            hdr = self.headers.get("X-Bridge-Override", "").strip().lower()
            if hdr:
                if not HEADER_OVERRULES:
                    override_mode = "enforced"
                elif hdr in ("off", "false", "0"):
                    override_mode = "off"

            ollama_req = translate_request(openai_req, effective_config,
                                           inject_defaults=override_mode != "off")
            model = ollama_req.get("model", "?")
            _log(f"[{_target_hostport(target)}] POST /v1/chat/completions model={model} stream={streaming} "
                 f"override={override_mode} options={ollama_req.get('options')}")

            try:
                ollama_resp = requests.post(
                    f"{target.url}/api/chat",
                    json=ollama_req,
                    stream=True,
                    timeout=(10, 600),
                )
            except requests.exceptions.ConnectionError:
                return False, "ConnectionError", None
            except requests.exceptions.Timeout:
                return False, "Timeout", None

            if ollama_resp.status_code != 200:
                err_body = ollama_resp.text
                
                # Spezielle Behandlung für 404 (Modell nicht gefunden)
                if ollama_resp.status_code == 404:
                    # Prüfe Header ODER Modellparameter [pull=true]
                    auto_pull_header = self.headers.get("X-Bridge-Auto-Pull", "").lower() in ("true", "1", "yes")
                    auto_pull_param = model_params.get("pull", "").lower() in ("true", "1", "yes")
                    auto_pull = auto_pull_header or auto_pull_param
                    
                    if auto_pull:
                        _log(f"[{_target_hostport(target)}] 404 received, attempting auto-pull for model '{model}'...")
                        success, error = _ollama_pull_model(target.url, model)
                        
                        if success:
                            _log(f"[{_target_hostport(target)}] Successfully pulled model '{model}', retrying request...")
                            try:
                                ollama_resp = requests.post(
                                    f"{target.url}/api/chat",
                                    json=ollama_req,
                                    stream=True,
                                    timeout=(10, 600),
                                )
                                if ollama_resp.status_code == 200:
                                    return True, None, ollama_resp
                                else:
                                    _log(f"[{_target_hostport(target)}] Retry failed with HTTP {ollama_resp.status_code}")
                            except Exception as e:
                                _log(f"[{_target_hostport(target)}] Retry failed: {e}")
                        else:
                            _log(f"[{_target_hostport(target)}] Auto-pull failed: {error}")
                            self._send_error(404, f"ollama pull {model}")
                            return True, None, None  # Signal that we handled the error
                    
                    # Kein Auto-Pull oder Auto-Pull fehlgeschlagen: Direkter 404-Fehler
                    self._send_error(404, f"ollama pull {model}")
                    return True, None, None  # Signal that we handled the error
                
                return False, f"HTTP {ollama_resp.status_code}", ollama_resp

            return True, None, ollama_resp

        start = time.time()
        target = None
        if forced_target:
            # Direct mode: skip failover logic
            target = forced_target
            success, error, resp = _send_request_to_target(target)
            if not success:
                if error == "ConnectionError":
                    self._send_error(502, f"ConnectionError zu {target.name}")
                elif error == "Timeout":
                    self._send_error(504, f"Timeout bei {target.name}")
                else:
                    self._send_error(502, error)
                return
            ollama_resp = resp
        else:
            # Normal mode: failover logic
            failed_urls = set()
            while True:
                target = self._select_target(failed_urls)
                if target is None:
                    self._send_error(502, "Kein Ollama verfuegbar")
                    return

                success, error, resp = _send_request_to_target(target)
                if success:
                    if resp is None:
                        # Error already sent by _send_request_to_target (e.g. 404)
                        return
                    ollama_resp = resp
                    break
                else:
                    failed_urls.add(target.url)
                    _log(f"[{_target_hostport(target)}] {error} -> failover")
                    continue

        # ── Response-Streaming / -Sammlung ────────────────────────
        # UPSTREAM timing + Stall-Watcher für direkte Ollama-Verbindung
        upstream_start_mono = time.monotonic()
        try:
            upstream_label = f"OLLAMA {_target_hostport(target)}"
        except Exception:
            upstream_label = "OLLAMA"
        upstream_watcher = UpstreamStallWatcher(
            f"direct-{threading.get_ident()}-{int(time.time()*1000)%100000}",
            label=upstream_label,
            low_tps_threshold=UPSTREAM_LOW_TPS,
        )
        upstream_watcher.start()
        upstream_watcher.record(0)

        def iter_ollama_chunks():
            for line in ollama_resp.iter_lines():
                upstream_watcher.record(len(line) if line else 0)
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

            downstream_dur = time.time() - start
            try:
                upstream_dur = time.monotonic() - upstream_start_mono
            except Exception:
                upstream_dur = downstream_dur
            try:
                tps = upstream_watcher.avg_tps_total() if 'upstream_watcher' in locals() else 0.0
                ev_cnt = upstream_watcher.total_events if 'upstream_watcher' in locals() else 0
                tps_str = f"{tps:.2f} TPS"
            except Exception:
                tps_str = "n/a"
                ev_cnt = "?"
            _log(f"[{_target_hostport(target)}] {'SSE' if streaming else 'JSON'} -> 200  upstream={upstream_dur:.2f}s ({tps_str}, {ev_cnt} events)  downstream={downstream_dur:.2f}s  total={downstream_dur:.2f}s")
            if downstream_dur > 10:
                _log(f"=== [TIMING] OLLAMA slow total {downstream_dur:.1f}s (upstream {upstream_dur:.1f}s, {tps_str}) ===")
            # final downgraded-Hinweis wenn sehr langsam aber nicht abgebrochen
            try:
                if tps < UPSTREAM_LOW_TPS and ev_cnt > 3:
                    _log(f"~~~ [SLOW] OLLAMA final avg {tps_str} über {upstream_dur:.1f}s - gedrosselt, Tokens transparent durchgereicht ~~~")
            except Exception:
                pass

        except BrokenPipeError:
            try:
                upstream_watcher.stop()
            except Exception:
                pass
            raise
        except Exception:
            try:
                upstream_watcher.stop()
            except Exception:
                pass
            _log(f"FEHLER in Response-Translation: {traceback.format_exc()}")
            try:
                self._send_error(500, "Interner Fehler bei der Response-Uebersetzung")
            except Exception:
                pass
        finally:
            try:
                upstream_watcher.stop()
            except Exception:
                pass

    # ── Queue-Modus ────────────────────────────────────────────

    def _handle_queue(self, raw_body, target_url, target_key, queue_mode,
                      session_id=None, prio=100,
                      size_tracker_enabled=True,
                      fallback_timeout=None):
        # DOWNSTREAM timing: Gesamtzeit vom Client-Request bis zur finalen Antwort
        downstream_start = time.monotonic()
        downstream_start_wall = time.time()
        if fallback_timeout is None:
            fallback_timeout = QUEUE_FALLBACK_TIMEOUT
        # JSON parsen
        try:
            openai_req = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_error(400, "Ungueltiges JSON")
            return
        
        streaming = openai_req.get("stream", True)

        if not target_url:
            self._send_error(422, "Keine Ziel-URL: X-Bridge-Target-URL oder QUEUE_TARGET_URL setzen")
            return

        if not target_key:
            self._send_error(422, "Kein API-Key: X-Bridge-Target-Key oder QUEUE_TARGET_KEY setzen")
            return

        # ── Original Body 1:1 weiterleiten (keine Übersetzung) ─────────
        # Modellname bereinigen (aber Request nicht übersetzen)
        model_name = openai_req.get("model", "")
        if model_name:
            clean_name, _ = _parse_model_name(model_name)
            openai_req["model"] = clean_name
        
        # Original Body beibehalten (nur Modellname bereinigen)
        original_payload = json.dumps(openai_req, ensure_ascii=False).encode()

        _ensure_queue()

        # ── SizeTracker Vor-Check ─────────────────────────────────────────
        # Nur relevant wenn 413-Learning aktiv UND Queue belegt (unter Last).
        # Queue leer → direkt an Upstream, keine Ratespiele.
        body_size = len(original_payload)
        queue_busy = _queue_scheduler.total > 0
        if size_tracker_enabled and queue_busy:
            if _size_tracker.is_too_large(target_url, target_key, body_size):
                min_size = _size_tracker.get_min_size(target_url, target_key)
                _log(f"[size-tracker] BLOCKED: {target_url[:50]}... "
                     f"body_size={body_size} > min_size={min_size}")
                self._send_error(413, f"Request too large (learned from previous 413: {min_size} bytes)")
                return
        elif not size_tracker_enabled:
            _log(f"[size-tracker] disabled (env/header) - kein Pre-Check")

        # ── RateLimitTracker: dynamischer Block bei 429 (Retry-After) ───────
        if target_key:
            cached = _rate_limit_tracker.get_error(target_url, target_key)
            if cached is not None:
                retry_after = _rate_limit_tracker.get_retry_after(target_url, target_key) or 60
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cached)))
                self.send_header("Retry-After", str(retry_after))
                self._send_cors()
                self.end_headers()
                self.wfile.write(cached)
                self.wfile.flush()
                _log(f"### [429 RATE-LIMIT] Client-Anfrage direkt mit 429 beantwortet — noch {retry_after}s geblockt (cached {len(cached)}B) ###")
                _log(f"### [429 RATE-LIMIT] Target {target_url[:50]}... Retry-After={retry_after}s ###")
                return

        entry = _QueueEntry(
            req_id=f"q-{threading.get_ident()}-{int(time.time()*1000)%100000}",
            group=_queue_group(openai_req) if streaming else "non-stream",
            stream=streaming,
            payload=original_payload,  # ← Original Request (1:1 weitergeleitet)
            target_url=target_url,
            target_key=target_key,
            prio=prio,
            session_id=session_id,
            retry_count=0,
            model_name=openai_req.get("model"),
            original_payload_size=len(raw_body),  # ← Ursprüngliche Größe
            size_tracker_enabled=size_tracker_enabled,
        )

        total = _queue_scheduler.total
        if not _queue_scheduler.submit(entry):
            self._send_error(429, f"Queue voll ({total}/{QUEUE_MAX_SIZE})")
            return

        pos = _queue_scheduler.total
        _log(f"[queue] submit entry={entry.id} pos={pos} group={entry.group[:12]} "
             f"target={target_url[:40]}... prio={prio} session={session_id}")

        # Keep-alive / SSE-Zuruecksendung an den Client
        try:
            # Erstes Item im Fallback-Fenster abwarten.
            # Fenster = QUEUE_FALLBACK_TIMEOUT (Default 15s), per Header übersteuerbar,
            # 0 = sofort 200 senden. Wenn das Item frueher kommt, wird der ECHTE
            # HTTP-Status der Upstream-Antwort genutzt (keine Ratespiele).
            first_item = _NO_ITEM
            try:
                first_item = entry.result.get(timeout=fallback_timeout)
            except queue.Empty:
                pass

            # Echten Status NACH dem Wait lesen (Worker setzt ihn vor dem ersten Item)
            upstream_status = entry.http_status or 200

            # Früh geantwortet + Fehler → echten Upstream-Status senden
            if isinstance(first_item, bytes) and upstream_status != 200:
                # Transparent: echter Upstream-Body + Content-Type, kein JSON-Kapsel
                body = entry.http_error_body if entry.http_error_body else first_item
                content_type = entry.http_content_type or "text/plain"
                self.send_response(upstream_status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if upstream_status == 429:
                    retry_after = _rate_limit_tracker.get_retry_after(entry.target_url, entry.target_key)
                    if retry_after is not None:
                        self.send_header("Retry-After", str(retry_after))
                self._send_cors()
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except Exception:
                    pass
                self.close_connection = True
                if upstream_status == 429:
                    _log(f"### [429 FORWARD] entry={entry.id} Upstream-429 direkt an Client weitergeleitet (Retry-After weitergegeben) ###")
                else:
                    _log(f"[queue] entry={entry.id} sent upstream status "
                         f"{upstream_status} to client")
                # DOWNSTREAM timing auch bei Fehler früh loggen (inkl TPS)
                try:
                    downstream_dur = time.monotonic() - downstream_start
                    upstream_dur = getattr(entry, "upstream_duration", None)
                    up_str = f"{upstream_dur:.2f}s" if isinstance(upstream_dur, (int, float)) else "n/a"
                    tps = getattr(entry, "upstream_avg_tps", None)
                    tps_str = f"{tps:.2f} TPS" if isinstance(tps, (int, float)) else "n/a"
                    _log(f"[queue] entry={entry.id} DONE  upstream={up_str} ({tps_str})  downstream={downstream_dur:.2f}s  total={downstream_dur:.2f}s (error {upstream_status})")
                except Exception:
                    pass
                return

            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._send_cors()
                self.end_headers()
                self.wfile.flush()

                sent_done = False

                # Erstes Item bereits im Fenster erhalten
                if isinstance(first_item, bytes):
                    self.wfile.write(first_item)
                    self.wfile.flush()
                elif first_item is None:
                    # Worker war vor dem 200er fertig (leerer Stream)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    sent_done = True

                if not sent_done:
                    while True:
                        if entry.cancel.is_set():
                            break
                        try:
                            item = entry.result.get(timeout=QUEUE_KEEPALIVE)
                        except queue.Empty:
                            # Keep-alive: Client hält Verbindung offen, kein Retry
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                            except Exception:
                                entry.cancel.set()
                            continue
                        if item is None:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                            sent_done = True
                            break
                        err_text = self._extract_error(item)
                        if err_text is not None:
                            # Nach-200-Fehler: Fehlergrund als SSE-Event mitsenden,
                            # dann Verbindung beenden (kein [DONE]).
                            ev = json.dumps({"error": err_text}).encode()
                            self.wfile.write(b"data: " + ev + b"\n\n")
                            self.wfile.flush()
                            self.close_connection = True
                            entry.cancel.set()
                            _log(f"[queue] entry={entry.id} post-200 error "
                                 f"(status={entry.http_status}), sent as SSE event, closing")
                            try:
                                downstream_dur = time.monotonic() - downstream_start
                                upstream_dur = getattr(entry, "upstream_duration", None)
                                up_str = f"{upstream_dur:.2f}s" if isinstance(upstream_dur, (int, float)) else "n/a"
                                tps = getattr(entry, "upstream_avg_tps", None)
                                tps_str = f"{tps:.2f} TPS" if isinstance(tps, (int, float)) else "n/a"
                                _log(f"[queue] entry={entry.id} DONE  upstream={up_str} ({tps_str})  downstream={downstream_dur:.2f}s (post-200 error)")
                            except Exception:
                                pass
                            return
                        self.wfile.write(item)
                        self.wfile.flush()

                if not sent_done:
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass

                # Verbindung sauber schliessen, damit der Client (AI-SDK oder requests)
                # den Stream-Abschluss erkennt und nicht auf Retry waehrt.
                self.close_connection = True
            else:
                # non-stream: echten Upstream-Status verwenden
                parts = []
                if isinstance(first_item, bytes):
                    parts.append(first_item)
                while True:
                    try:
                        item = entry.result.get(timeout=QUEUE_KEEPALIVE)
                    except queue.Empty:
                        continue
                    if item is None:
                        break
                    parts.append(item)
                body = b"".join(parts) if parts else b"{}"
                if upstream_status != 200:
                    # Transparent: echter Upstream-Body, sonst JSON-Fallback
                    if entry.http_error_body is not None:
                        body = entry.http_error_body
                    self.close_connection = True
                content_type = entry.http_content_type if upstream_status != 200 \
                    else "application/json"
                self.send_response(upstream_status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if upstream_status == 429:
                    retry_after = _rate_limit_tracker.get_retry_after(entry.target_url, entry.target_key)
                    if retry_after is not None:
                        self.send_header("Retry-After", str(retry_after))
                self._send_cors()
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            # DOWNSTREAM + UPSTREAM Gesamtzeit loggen (inkl TPS)
            try:
                downstream_dur = time.monotonic() - downstream_start
                upstream_dur = getattr(entry, "upstream_duration", None)
                up_str = f"{upstream_dur:.2f}s" if isinstance(upstream_dur, (int, float)) else "n/a"
                tps = getattr(entry, "upstream_avg_tps", None)
                tps_str = f"{tps:.2f} TPS" if isinstance(tps, (int, float)) else "n/a"
                ev_cnt = getattr(entry, "upstream_events", "?")
                _log(f"[queue] entry={entry.id} DONE  upstream={up_str} ({tps_str}, {ev_cnt} events)  downstream={downstream_dur:.2f}s  total={downstream_dur:.2f}s")
                # zusätzliche optisch sichtbare Trennlinie bei langer Dauer
                if downstream_dur > 10:
                    try:
                        up_val = f"{upstream_dur:.1f}s" if isinstance(upstream_dur, (int, float)) else "n/a"
                    except Exception:
                        up_val = "n/a"
                    _log(f"=== [TIMING] entry={entry.id} slow total {downstream_dur:.1f}s (upstream {up_val}, {tps_str}) ===")
            except Exception:
                _log(f"[queue] done entry={entry.id}")

        except (BrokenPipeError, ConnectionResetError):
            try:
                downstream_dur = time.monotonic() - downstream_start
                _log(f"[queue] client disconnected entry={entry.id} after {downstream_dur:.2f}s downstream")
            except Exception:
                _log(f"[queue] client disconnected entry={entry.id}")
            try:
                entry.cancel.set()
            except Exception:
                pass
            if _queue_scheduler is not None:
                try:
                    _queue_scheduler.remove(entry)
                except Exception:
                    pass
        finally:
            try:
                # finale DOWNSTREAM-Zeit auch bei frühem return (falls noch nicht geloggt)
                # nur loggen wenn entry existiert und noch nicht DONE geloggt wurde
                pass
            except Exception:
                pass
            try:
                entry.cancel.set()
            except Exception:
                pass
            if _queue_scheduler is not None:
                try:
                    _queue_scheduler.remove(entry)
                except Exception:
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
    if QUEUE_ENABLED:
        print(f"Queue-Mode: ENABLED  default_mode={QUEUE_MODE_DEFAULT}", flush=True)
        print(f"  target_url={QUEUE_TARGET_URL or '(not set, per X-Bridge-Target-URL)' }", flush=True)
        print(f"  keepalive={QUEUE_KEEPALIVE}s    max_size={QUEUE_MAX_SIZE}", flush=True)
        print(f"  fallback_timeout={QUEUE_FALLBACK_TIMEOUT}s    size_tracker={BRIDGE_SIZE_TRACKER}", flush=True)
        print(f"  upstream_pause={UPSTREAM_PAUSE}s    connect_timeout={QUEUE_CONNECT_TIMEOUT}s stream_timeout={QUEUE_STREAM_TIMEOUT}s", flush=True)
    print(flush=True)

    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer gestoppt.")
        server.server_close()


if __name__ == "__main__":
    import sys
    if "--test-parse" in sys.argv:
        # Einfacher Test für die Kommandozeile
        test_cases = [
            "qwen3.5:9b",
            "qwen3.5:9b[Prio=1]",
            "qwen3.5:9b[Prio=1;session-id=abc123]",
            "deepseek-v4-flash[num_ctx=32768;temperature=0.7;Prio=5]",
            "model[]",
            "openai/gpt-4o[Prio=1]",
        ]
        
        for test in test_cases:
            name, params = _parse_model_name(test)
            print(f"Input:  {test}")
            print(f"Output: ({name}, {params})")
            print()
    else:
        main()
