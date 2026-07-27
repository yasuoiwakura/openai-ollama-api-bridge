# py-ollama-openai-bridge

A lightweight HTTP proxy that translates OpenAI `/v1/chat/completions` requests to Ollama's native `/api/chat` API and back.

## The Problem

Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) **ignores `num_ctx` and most other parameters**. Every request spins up a new runner locked to 4096 context tokens – regardless of the model's actual context window.

The only workaround without this proxy: creating **custom Modelfiles** for every single model with `PARAMETER num_ctx 64000` baked in. 20 models = 20 Modelfiles. Change your mind on the context size? Rebuild all 20.

## What this proxy does

Uses Ollama's native `/api/chat` endpoint directly, where all parameters are respected. One `.env` file configures every model at once.

| Problem | Without bridge | With bridge |
|---|---|---|
| Context stuck at 4K | Custom Modelfile per model | `DEFAULT_NUM_CTX=64000` in `.env` |
| OpenCode caps output at 32K | Unfixable from client side | `DEFAULT_NUM_PREDICT=128000` overrides it |
| No parameter tuning | Modelfile per model | `DEFAULT_TEMPERATURE`, `DEFAULT_TOP_P`, etc. |
| Tool calls broken | OpenAI adapter mangles format | Correct translation both ways |
| Reasoning/thinking lost | Not exposed through adapter | Passed through transparently |

## Quick Start

### Docker (recommended)

```bash
git clone <repo>
cd py-ollama-openai-bridge
cp .env.example .env
# edit .env: set OLLAMA_URL to your Ollama host
docker compose up -d
# Bridge available at http://localhost:8080/v1
```

### Direct (without Docker)

```bash
pip install -r requirements.txt
# configure .env
python proxy.py
```

Point OpenCode at the bridge:
```json
{
  "provider": {
    "ollama-bridge": {
      "name": "Ollama (bridged)",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://localhost:8080/v1"
      },
      "models": {
        "gpt-oss:20b": {
          "name": "_gpt-oss:20b",
          "tool_call": true,
          "limit": { "context": 64000, "output": 40960 }
        }
      }
    }
  }
}
```

## All options

```env
NUM_CTX=64000         # Context window (tokens)
NUM_PREDICT=128000    # Max output tokens
TEMPERATURE=0.7       # Sampling temperature
TOP_P=0.9             # Nucleus sampling
TOP_K=40              # Top-K sampling
MIN_P=0.05            # Minimum probability
REPEAT_PENALTY=1.1    # Repeat penalty
SEED=42               # Random seed
KEEP_ALIVE=30m        # Model stay-in-memory duration
```

Only uncommented values in `.env` are injected. Unset = Ollama's default.

## Failover Routing

Zwei Ollama-Instanzen: `OLLAMA_URL` (Haupt) -> `FAILOVER_OLLAMA_URL` (Backup).

```env
OLLAMA_URL=http://192.168.0.42:11434

FAILOVER_OLLAMA_URL=http://192.168.0.101:11434
FAILOVER_NUM_CTX=96000
FAILOVER_NUM_PREDICT=128000
FAILOVER_KEEP_ALIVE=5m
```

- Gesundheitscheck via `GET /api/tags` vor jedem Routing
- Haupt nicht erreichbar -> automatischer Failover zu Backup
- Auch bei ConnectionError/Timeout während laufendem Chat -> Failover
- Failover-Target hat eigene Parameter (num_ctx, num_predict, keep_alive)
- Einfach-Modus: nur `OLLAMA_URL` setzen (abwärtskompatibel)

## Architecture

```
OpenCode → POST /v1/chat/completions → bridge → POST /api/chat → Ollama
           ←  SSE stream ←                 ←  NDJSON ←
```
