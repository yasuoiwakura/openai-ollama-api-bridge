# SPEC.md

# py-ollama-openai-bridge

## Goal

Implement a lightweight HTTP bridge that exposes an OpenAI-compatible `/v1/chat/completions` endpoint while communicating with Ollama using its native API.

The primary motivation is to overcome limitations of Ollama's OpenAI compatibility layer when used with OpenCode.

The bridge should be transparent for OpenAI-compatible clients while allowing native Ollama features to be injected where necessary.

---

# Problem Statement

The following behaviour has been reproduced.

## Environment

- OpenCode
- Ollama (Linux Docker, ROCm)
- Remote access over HTTP
- OpenAI-compatible endpoint (`/v1/chat/completions`)

Models tested:

- gpt-oss:20b
- other tool-capable models

---

## Observation

The model itself supports large contexts.

Example:

```
ollama show gpt-oss:20b

context length: 131072
tools: yes
thinking: yes
```

However the loaded runner differs.

```
ollama ps --json
```

returns

```
context_length: 4096
```

when loaded through OpenAI Chat Completions.

---

Loading exactly the same model through the native Ollama API:

```
POST /api/chat

{
    "options": {
        "num_ctx": 32768
    }
}
```

results in

```
context_length: 32768
```

Therefore:

- the model supports large contexts
- Ollama supports large contexts
- only the OpenAI compatibility endpoint loads a runner using the default context

---

## Important discovery

If a model is first loaded through `/api/chat` using a larger `num_ctx`

and later a request arrives via

```
/v1/chat/completions
```

Ollama unloads the existing runner and creates a new one using

```
context_length = 4096
```

Therefore pre-loading the model is NOT sufficient.

The bridge must translate requests instead of relying on preloading.

---

# Project Scope

Implement only what is required for OpenCode compatibility.

This is NOT intended to become a full OpenAI implementation.

---

# High Level Architecture

## Production Flow

```
OpenCode
      │  /v1/chat/completions
      ▼
py-ollama-openai-bridge
      │  /api/chat
      ▼
Ollama
```

The bridge exposes `POST /v1/chat/completions` and internally calls `POST /api/chat`.

## Test Setup (aktuell)

Für Entwicklung und Debugging werden zwei mitmproxy-Instanzen als transparente Logging-Layer dazwischengeschaltet:

```
OpenCode
  │  @ai-sdk/openai-compatible → http://192.168.0.42:18070/v1
  ▼
mitmweb1 (Docker)     Port 18070 ← Host → 18071 (Web UI)
  │  mode reverse: http://192.168.0.101:8080
  │  ── logging only, transparent ──
  ▼
py-ollama-openai-bridge
  │  OLLAMA_URL = http://192.168.0.42:18080
  │  /api/chat
  ▼
mitmweb2 (Docker)     Port 18080 ← Host → 18081 (Web UI)
  │  mode reverse: http://192.168.0.42:11434
  │  ── logging only, transparent ──
  ▼
Ollama :11434
```

Beide mitmproxy-Instanzen verändern Requests/Responses nicht – sie zeichnen lediglich den Traffic auf. Der Bridge (`proxy.py`) läuft auf `192.168.0.101:8080`.

---

# Translation

Incoming:

```
OpenAI Chat Completions
```

↓

Convert to

```
Ollama /api/chat
```

↓

Inject additional options

Example

```json
"options": {
    "num_ctx": 32768
}
```

↓

Receive streamed response

↓

Convert back into OpenAI-compatible SSE chunks.

---

# Requirements

## Functional

- OpenAI compatible endpoint

```
POST /v1/chat/completions
```

- Support streaming
- Support non-streaming
- Forward all messages
- Preserve system prompts
- Preserve assistant messages
- Preserve tool messages
- Preserve tool calls
- Preserve reasoning fields whenever possible

---

## Ollama Options

Support configurable injection of:

- num_ctx
- temperature
- top_p
- top_k
- repeat_penalty
- num_predict
- seed
- keep_alive

Configuration should be external.

---

# Configuration

Example

```yaml
ollama:
  url: http://localhost:11434

defaults:
  num_ctx: 32768
  keep_alive: 30m
  temperature: 0.7
```

---

# Non Goals

Do NOT

- implement authentication
- implement rate limiting
- implement billing
- implement user management
- implement conversation storage

The bridge should remain stateless.

---

# Future Features

## Premium/Failover Routing

### Motivation

OpenCode-Anfragen profitieren von größerem Context (bessere Antwort-Qualität, weniger
Kompaktierung). Falls eine Workstation mit mehr VRAM (32GB) verfügbar ist, sollen Anfragen
dorthin mit höherem `num_ctx` geroutet werden. Ist die Workstation offline, geht die Anfrage
an den Standard-Ollama (16GB) mit 64K Context.

### Anforderung

- Zwei Ollama-Targets: **Premium** (bevorzugt) und **Fallback** (immer)
- Jedes Target hat eigene Parameter (URL, num_ctx, num_predict, keep_alive)
- Healthcheck vor jedem Routing: `GET /api/tags` mit kurzem Timeout
- Premium nicht erreichbar → automatischer Failover zu Fallback
- Auch während laufendem Chat bei ConnectionError/Timeout: Failover zum nächsten Target
- Single-Target-Modus bleibt abwärtskompatibel erhalten (nur `OLLAMA_URL` gesetzt)

### .env-Design

```env
#─ PREMIUM / FAILOVER ──────────────────────────────────────────
# Premium Target (z.B. Workstation RTX 32GB)
PREMIUM_ENABLED=true
PREMIUM_URL=http://192.168.0.101:11434
PREMIUM_NUM_CTX=96000
PREMIUM_NUM_PREDICT=128000
PREMIUM_KEEP_ALIVE=5m
PREMIUM_TIMEOUT=5

# Fallback Target (z.B. AMD 16GB)
FALLBACK_URL=http://192.168.0.42:11434
FALLBACK_NUM_CTX=64000
FALLBACK_NUM_PREDICT=128000
FALLBACK_KEEP_ALIVE=5m

# Abwärtskompatibel:
# OLLAMA_URL=http://192.168.0.42:11434
```

### Routing-Logik

```
Request → Target Selection → 1. Premium (Healthcheck /api/tags, 5s)
                                ├─ OK → Premium-Konfiguration (num_ctx=96000)
                                └─ Fail → 2. Fallback (Healthcheck /api/tags, 10s)
                                           ├─ OK → Fallback-Konfiguration (num_ctx=64000)
                                           └─ Fail → 502 "Kein Ollama verfügbar"

Bei ConnectionError/Timeout während Chat: nächstes Target versuchen.
Non-Chat-Endpoints → Premium bevorzugt, sonst Fallback.
```

### Umsetzung

1. Config-Block `TARGETS[]` aus `.env` lesen (ersetzt `OLLAMA_URL`+`BRIDGE_CONFIG`)
2. `select_target()`: Targets sortiert nach Priority healthchecken
3. `_handle_chat_completions`: Target vor Request wählen, Failover bei Fehler
4. `_proxy_pass`: Premium bevorzugen, sonst Fallback
5. Logging: Target-Name + Config in jedem Log-Eintrag
6. Server-Start: Alle Targets mit Status anzeigen

### Abgrenzung zu Parametersets

Parametersets (`PS1_*`) steuern Sampling-Parameter pro Modell-Name, unabhängig vom Target.
Premium/Failover steuert das Routing zu verschiedenen Ollama-Instanzen mit eigenem `num_ctx`.
Beide Features ergänzen sich und können gleichzeitig genutzt werden.

---

## Parametersets

Aktuell werden nur `num_ctx` und `num_predict` global via `.env` gesetzt.

Als Erweiterung können **Parametersets** definiert werden, die beliebige Ollama-Optionen bündeln
und per Substring im Modellnamen aktiviert werden. Der Substring wird vor dem Weiterleiten an
Ollama aus dem Modellnamen entfernt.

### Beispiel

```env
#── Parameterset "1": schnelle Antworten ──
PS1_TEMPERATURE=0.3
PS1_TOP_P=0.8
PS1_TOP_K=20

#── Parameterset "2": kreativ ──
PS2_TEMPERATURE=1.2
PS2_TOP_P=0.95
PS2_TOP_K=80
```

Der Client wählt ein Set über den Modellnamen:
```
qwen3.5:9b[1]    → aktiviert PS1_TEMPERATURE/PS1_TOP_P/PS1_TOP_K
                    Modellname an Ollama: qwen3.5:9b

gamemaker1/gemma3:12b-fc[2] → aktiviert PS2_*
                                Modellname an Ollama: gamemaker1/gemma3:12b-fc
```

### Vorteile gegenüber Modelfiles

- Ein Parameterset kann für **mehrere Modelle** genutzt werden
- Kein Neuerstellen von Modellen nötig
- Änderungen sofort wirksam (kein `ollama create`)
- Parametersets können pro Client/Session gewechselt werden

### Umsetzung

1. Proxy parst `.env` auf `PS<NR>_<OPTION>`-Pattern
2. Extrahiert `[<NR>]` aus dem Modellnamen
3. Merged die Options des passenden Sets in den Request
4. Entfernt `[<NR>]` aus dem Modellnamen vor Übergabe an Ollama

---

## Multithreading

**Priorität: sehr niedrig** – lösbar via Kubernetes/Docker Compose Replicas.

### Problem

Der aktuelle Proxy verwendet `HTTPServer` mit `BaseHTTPRequestHandler`, der Requests
synchron und single-threaded abarbeitet. Bei mehreren parallelen Requests blockiert
ein Request den nächsten.

### Lösungsidee

- `ThreadingHTTPServer` (Python 3.7+) oder `ForkingHTTPServer` nutzen
- Oder `socketserver.ThreadingMixIn` für einfaches Multithreading
- Oder auf ASGI/uvicorn wechseln (async, aber großer Umbau)

### Warum niedrige Priorität

- Im OpenCode-Kontext kommt selten mehr als 1 Request parallel
- Docker Compose kann via `--scale ollama-bridge=3` mehrere Instanzen starten
- K8s/ Nomad lösen das auf Orchestrierungsebene
- Single-Threaded ist simpler, fehlerresistenter und leichter zu debuggen

---

# Logging

Useful structured logging:

- incoming model
- translated endpoint
- injected options
- response time
- errors

Never log prompt contents by default.

---

# Desired Properties

- lightweight
- dependency minimal
- transparent
- production friendly
- easy to debug
- easy to extend

---

# Initial Milestone

Version 0.1 should support:

- one Ollama backend
- `/v1/chat/completions`
- streaming
- configurable `num_ctx`
- configurable `keep_alive`
- transparent message forwarding
Once this works with OpenCode, additional OpenAI endpoints may be added later.
---
## References
- `README.md` – Project overview, quick start guide, Docker usage, configuration examples.
- `SPEC.md` – Detailed technical specification, API mapping, multithreading, logging, parametersets, milestones.
- `testdata/MAPPING.md` – Full mapping between OpenAI Chat Completions and Ollama Chat APIs, including field conversions and streaming details.
- `NOTES.md` – Template & notes for configuring opencode.json, parameter explanations, pitfalls, VRAM rule of thumb.
All these files provide complementary information: the README is user‑facing, SPEC contains formal specs, MAPPING gives implementation reference, and NOTES offers configuration guidance for developers using OpenCode.