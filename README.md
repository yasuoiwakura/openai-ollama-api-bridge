# py-ollama-openai-bridge

A lightweight HTTP proxy that translates OpenAI `/v1/chat/completions` requests to Ollama's native `/api/chat` API and back.

## The Problem

Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) has serious runtime configuration limitations.

Without custom Modelfiles, parameters such as context size cannot be centrally controlled. Requests through the OpenAI compatibility layer may run with Ollama defaults instead of the desired runtime settings.

The typical workaround is creating **custom Modelfiles** for every single model with settings like:

```
PARAMETER num_ctx 64000
```

This creates duplicated model definitions:

- `llama3.2` → base model + custom Modelfile
- `qwen3` → base model + custom Modelfile
- `gemma3` → base model + custom Modelfile
- `mistral` → base model + custom Modelfile
- `deepseek-r1` → base model + custom Modelfile

20 models = 20 additional Modelfiles.

Changing the context size later means rebuilding or updating all of them again.

The problem is not the models themselves — it is the missing central runtime configuration layer.

### Without creating Ollama Modelfiles, the default OpenCode + Ollama setup has serious compatibility limitations

```mermaid
flowchart LR

    subgraph OpenCode
        OC_OpenAI["OpenAI Connector"]
        subgraph JSON["opencode.jsonc"]
            JMAX["max_tokens<br />=64K"]
            JMAXLEN["max response length"]
            J_MODEL["basic model"]
        end

        subgraph OC_POST["POST"]
            OC_COMPLETE["/v1/chat/completions"]
            MODEL["basic model"]
            MSG["messages"]
            MAX["max_tokens<br />=32K"]
            STREAM["stream"]
        end
    end

    subgraph Ollama

        CTX_DEFAULT["Default:<br />num_ctx=4K"]

        subgraph OL_OpenAI["OpenAI Compatibility API"]
            OL_COMPLETE["/v1/chat/completions"]
            OL_MODEL["basic model"]
            CMSG["messages"]
            CMAX["max_tokens<br />=32K"]
            CSTREAM["stream"]
        end

        subgraph Native["Native Ollama APIs"]
            CHAT["POST /api/chat"]
            GEN["POST /api/generate"]
        end

        MODELS[(Any Model **without** Modelfile)]

    end

    J_MODEL --> MODEL

    JMAX -.->|"🔒<br />hardcoded<br />32K"| MAX
    JMAXLEN -.->|"❌<br />ignored"| MAX

    OC_OpenAI -.->|"❌<br />not used"| Native
    OC_COMPLETE -.->|"🚧limited"| OL_COMPLETE

    OC_OpenAI --> OC_POST

    MODEL --> OL_MODEL
    MSG --> CMSG
    MAX --> CMAX
    STREAM --> CSTREAM

    OL_MODEL --Default Model<br/>num_ctx=4K<br/>without Modelfile--> MODELS

    CMSG --> MODELS

    CMAX -.->|"❌<br />ignored"| CTX_DEFAULT

    CTX_DEFAULT -->|"without custom Modelfile"| MODELS

    CSTREAM --> MODELS

    CHAT --> MODELS
    GEN --> MODELS


    style OC_COMPLETE fill:#ffe7aa
    style OL_COMPLETE fill:#ffe7aa
    style CHAT fill:#b5f5b5
    style GEN fill:#b5f5b5
    style JMAX fill:#b5f5b5
    style MAX fill:#ffe7aa
    style CMAX fill:#ffaaaa
    style J_MODEL fill:#b5f5b5
    style OL_MODEL fill:#b5f5b5
    style MODEL fill:#b5f5b5
```

---

## Why create another custom Modelfile for every model?

The only practical workaround without a bridge is creating a custom Ollama Modelfile for every model just to override runtime parameters.

This is redundant, time-consuming, and difficult to maintain when frequently testing or switching models.

```mermaid
flowchart TD

    M1["llama3.2<br/>Model"]
    F11["Base Model<br/>llama3.2"]
    F12["Modelfile<br/>llama3.2<br/>num_ctx=32K"]

    M2["qwen3<br/>Model"]
    F21["Base Model<br/>qwen3"]
    F22["Modelfile<br/>qwen3<br/>num_ctx=32K"]

    M3["gemma3<br/>Model"]
    F31["Base Model<br/>gemma3"]
    F32["Modelfile<br/>gemma3<br/>num_ctx=32K"]

    M4["mistral<br/>Model"]
    F41["Base Model<br/>mistral"]
    F42["Modelfile<br/>mistral<br/>num_ctx=32K"]

    M5["deepseek-r1<br/>Model"]
    F51["Base Model<br/>deepseek-r1"]
    F52["Modelfile<br/>deepseek-r1<br/>num_ctx=32K"]

    M1 --> F11
    M1 --> F12

    M2 --> F21
    M2 --> F22

    M3 --> F31
    M3 --> F32

    M4 --> F41
    M4 --> F42

    M5 --> F51
    M5 --> F52

    style M1 fill:#b5f5b5
    style M2 fill:#b5f5b5
    style M3 fill:#b5f5b5
    style M4 fill:#b5f5b5
    style M5 fill:#b5f5b5

    style F12 fill:#ffe7aa
    style F22 fill:#ffe7aa
    style F32 fill:#ffe7aa
    style F42 fill:#ffe7aa
    style F52 fill:#ffe7aa
```
## What this proxy does

Uses Ollama's native `/api/chat` endpoint directly, where runtime parameters are explicitly supported.

One `.env` file configures all models centrally.

| Problem | Without bridge | With bridge |
|---|---|---|
| Context stuck at default | Custom Modelfile per model | `NUM_CTX=64000` in `.env` |
| Output token limits | Client-side limitations | `NUM_PREDICT=128000` override |
| Runtime tuning | Modelfile per model | Central `.env` configuration |
| Tool calls | OpenAI adapter translation issues | Native Ollama format translation |
| Thinking/reasoning fields | Adapter dependent | Passed through transparently |

---

## Central API bridge to enforce runtime settings

Provide a single OpenAI endpoint while centrally enforcing runtime policies and automatically switching to a secondary Ollama server if the primary becomes unavailable.

```mermaid
flowchart LR

    Client["OpenCode<br/>OpenAI Connector"]

    subgraph Bridge["API Bridge"]
        TRANS["OpenAI → Ollama<br/>Translation"]
        CONTEXT["Enforce<br/>Context Size Policy"]
        FAILOVER["Switch to other Server"]
    end

    O1["Ollama Server"]

    Client -->|"speaks OpenAI API"| Bridge
    Bridge -->|"speaks Ollama API"| O1

    style Client fill:#ffaaaa
    style Bridge fill:#88FFFF
    style O1 fill:#b5f5b5
```

---

## High Availability / Failover

Provide a single OpenAI endpoint while automatically switching to a secondary Ollama server if the primary becomes unavailable.

```mermaid
flowchart LR


    Client["OpenCode<br/>OpenAI Connector"]

    Bridge["OpenAI ↔ Ollama API Bridge"]

    Decision{"Primary Available?"}

    C1["Context Size = 128K"]
    C2["Context Size = 32K"]

    O1["Primary Ollama Server"]
    O2["Secondary Ollama Server"]

    Client --> Bridge
    Bridge --> Decision

    Decision -->|Yes| C1
    Decision -->|No| C2

    C1 --> O1
    C2 --> O2



    style Bridge fill:#ffff88
    style O1 fill:#b5f5b5
    style O2 fill:#b5f5b5
    style C1 fill:#e8f4fd
    style C2 fill:#88FFFF
```

---

## Quick Start

### Docker (recommended)

```bash
git clone <repo>
cd py-ollama-openai-bridge
cp .env.example .env

# edit .env: set OLLAMA_URL to your Ollama host

docker compose up -d
```

Bridge available at:

```text
http://localhost:8080/v1
```

---

### Direct (without Docker)

```bash
pip install -r requirements.txt

# configure .env

python proxy.py
```

---

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
          "limit": {
            "context": 64000,
            "output": 40960
          }
        }
      }
    }
  }
}
```

---

## All options

```env
NUM_CTX=64000
NUM_PREDICT=128000

TEMPERATURE=0.7
TOP_P=0.9
TOP_K=40
MIN_P=0.05
REPEAT_PENALTY=1.1

SEED=42
KEEP_ALIVE=30m
```

Only uncommented values in `.env` are injected.

Unset values use Ollama defaults.

---

## Failover Routing

Two Ollama instances:

```
OLLAMA_URL → FAILOVER_OLLAMA_URL
```

Example:

```env
OLLAMA_URL=http://192.168.0.42:11434

FAILOVER_OLLAMA_URL=http://192.168.0.101:11434

FAILOVER_NUM_CTX=96000
FAILOVER_NUM_PREDICT=128000
FAILOVER_KEEP_ALIVE=5m
```

Features:

- Health check via `GET /api/tags`
- Automatic switch when primary is unavailable
- Failover on connection errors and timeouts
- Independent failover parameters
- Simple mode: only `OLLAMA_URL` required

---

## API Comparison

| Feature | OpenCode w/ Ollama | API Bridge |
|---|---|---|
| OpenAI Chat Completions | ✅ | ✅ |
| OpenAI Responses API | ⚠️ Limited | ✅ |
| Native `/api/chat` | ❌ | ✅ |
| Native `/api/generate` | ❌ | ✅ |
| Streaming | ✅ | ✅ |
| Tool Calling | Backend dependent | Preserved |
| Multiple Ollama Servers | ❌ Manual switch | ✅ |
| Context Size Policy | ❌ Per-model Modelfiles | ✅ Centralized runtime policy |
| Automatic Failover | ❌ | ✅ |
| Routing Policies | ❌ | ✅ |
| Single Stable Endpoint | ❌ | ✅ |

---

## Runtime configuration model

Instead of creating model-specific Modelfiles:

```
llama3.2 + Modelfile
qwen3    + Modelfile
gemma3   + Modelfile
mistral  + Modelfile
deepseek + Modelfile
```

the bridge applies runtime policies centrally:

```
.env
 |
 +-- NUM_CTX
 +-- NUM_PREDICT
 +-- TEMPERATURE
 +-- TOP_P
 +-- TOP_K
 +-- KEEP_ALIVE
 |
 v
All Ollama models
```

This allows changing runtime behaviour without rebuilding model definitions.

---

## Design goals

- Keep OpenAI-compatible clients unchanged
- Use Ollama's native API capabilities
- Avoid duplicated Modelfiles
- Centralize runtime configuration
- Support multiple Ollama backends
- Provide automatic failover
- Preserve streaming responses
- Preserve tool calls and reasoning metadata where available

---

## License

MIT