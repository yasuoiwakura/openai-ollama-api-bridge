# Template & Notes


## opencode
run this in powershell if you want websearch for local AI models:
`[Environment]::SetEnvironmentVariable("OPENCODE_ENABLE_EXA", "1", "User")`
- some models will not use websearch while in plan more

### Base template for Ollama via the Proxy opencode.json

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "disabled_providers": [],
  "provider": {
    "mitm-python-ollama": {
      "name": "mitm-python-ollama",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://192.168.0.42:18070/v1"
      },
      "models": {
        "_gpt-oss:20b": {
          "name": "_gpt-oss:20b",
          "tool_call": true,
          "attachment": true,
          "reasoning": true,
          "temperature": true,
          "limit": {
            "context": 64000,
            "output": 40960
          }
        },
        "qwen3.5:9b": {
          "name": "qwen3.5:9b",
          "tool_call": true,
          "attachment": true,
          "reasoning": true,
          "temperature": true,
          "modalities": {
            "input": ["text"],
            "output": ["text"]
          },
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

### Parameter Explanation

| Field | Value | Effect |
|---|---|---|
| `tool_call: true` | Required | Without it, tool support is missing |
| `attachment: true` | Required | Otherwise file attachments are blocked |
| `reasoning: true` | Optional | Enables Thinking/Reasoning UI; does not cause issues |
| `temperature: true` | Required | Otherwise the temperature from the Agent config is ignored |
| `modalities.input` | `"[\"text\"]"` | For text models. Only set to `[\"text\", \"image\"]` if the model actually supports images (e.g., LLaVA, Qwen2.5-VL) |
| `limit.context` | 64000 | Must match `DEFAULT_NUM_CTX` in the Proxy (64K). Controls when OpenCode compresses |
| `limit.output` | 40960 | Schema requirement. OpenCode internally caps at 32K; the Proxy overrides via `DEFAULT_NUM_PREDICT` |

## Pitfalls

- **Never set `limit.input`** – it overrides `limit.context` and delays compaction. The model then receives more tokens than its context window (`num_ctx`) allows.
- **`limit.context` must be <= `DEFAULT_NUM_CTX`** (Proxy `.env`) or OpenCode will compress too late.
- **The `name` field** is only for sorting/display in the model selection UI. Models prefixed with `_` appear at the top.
- **`$schema`** enables auto-completion in editors.

## VRAM Rule of Thumb

| Context (num_ctx) | Qwen3.5:9b | gpt-oss:20b |
|---|---|---|
| 32K | ~8 GB | ~10 GB |
| 64K | ~10 GB | ~14 GB |
| 128K | ~16 GB | ❌ not possible |

---

## Queue Mode (Serialisierung gegen Rate-Limits)

Wenn ein Upstream (z.B. cheapestinference.com) nur **1 parallelen Request** erlaubt,
kann die Bridge mehrere parallele OpenCode-Requests **in einer Warteschlange** auffangen
und nacheinander weiterreichen. Die Verbindung zum Client bleibt per Keep-alive offen,
damit OpenCode keinen Retry auslöst.

### opencode.jsonc — Direkt zu CheapestInference (ohne Bridge)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "cheapestinference": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CheapestInference",
      "options": {
        "baseURL": "https://api.cheapestinference.com/v1",
        "apiKey": "sk-DEIN-CHEAPESTINFERENCE-KEY"
      },
      "models": {
        "kimi-k3":           { "name": "Kimi K3" },
        "glm-5.2":           { "name": "GLM 5.2" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" },
        "mimo-v2.5":         { "name": "MiMo v2.5" }
      }
    }
  }
}
```

> **Nachteil:** Nur 1 parallele Sitzung möglich. Bei mehreren parallelen Requests
> (mehrere OpenCode-Fenster oder Tool-Call-Ketten) kommt es zu 429/Timeouts.
> OpenCode löst Retry aus → Instabilität.

### opencode.jsonc — Über Bridge mit Queue (empfohlen)

Die Bridge serialisiert mehrere parallele Requests auf 1 parallelen Upstream-Request
und hält die Verbindung per Keep-alive offen (kein OpenCode-Retry).

**WICHTIG:** Der API-Key muss über den Header kommen (nicht in .env oder Git):
- `X-Bridge-Target-Key`: Der cheapestinference API-Key
- `X-Bridge-Target-URL`: Die Ziel-URL (oder aus .env `QUEUE_TARGET_URL`)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "cheapestinference-queued": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CheapestInference (queued)",
      "options": {
        "baseURL": "http://192.168.0.42:8080/v1",
        "headers": {
          "X-Bridge-Queue": "on",
          "X-Bridge-Target-URL": "https://api.cheapestinference.com/v1",
          "X-Bridge-Target-Key": "sk-DEIN-CHEAPESTINFERENCE-KEY"
        }
      },
      "models": {
        "kimi-k3":           { "name": "Kimi K3" },
        "glm-5.2":           { "name": "GLM 5.2" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" },
        "mimo-v2.5":         { "name": "MiMo v2.5" }
      }
    }
  }
}
```

> **Bridge-Headless/VPS:** Steht die Bridge auf einem Server, zeigt `baseURL` dorthin:
> `"baseURL": "http://DEIN-SERVER:8080/v1"`.

### opencode.jsonc — Bridge mit Queue + Key aus Umgebungsvariable (optional)

Falls der Key aus einer Umgebungsvariable kommen soll (z. B. `CHEAPESTINFERENCE_API_KEY`),
kann opencode.jsonc dies über `{env:...}` lösen:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "cheapestinference-queued": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CheapestInference (queued)",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "headers": {
          "X-Bridge-Queue": "on",
          "X-Bridge-Target-URL": "https://api.cheapestinference.com/v1",
          "X-Bridge-Target-Key": "{env:CHEAPESTINFERENCE_API_KEY}"
        }
      },
      "models": {
        "kimi-k3":           { "name": "Kimi K3" },
        "glm-5.2":           { "name": "GLM 5.2" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    }
  }
}
```

```bash
# In der Shell/Session setzen:
export CHEAPESTINFERENCE_API_KEY=sk-DEIN-CHEAPESTINFERENCE-KEY
```

### opencode.jsonc — Nur Ollama (bestehend, unverändert)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama-bridge": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (bridged)",
      "options": {
        "baseURL": "http://localhost:8080/v1"
      },
      "models": {
        "qwen3.5:9b": {
          "name": "_qwen3.5:9b",
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

### Header-Übersicht

| Header | Pflicht | Effekt |
|---|---|---|
| `X-Bridge-Queue` | Ja (`on`) | Queue-Modus aktivieren |
| `X-Bridge-Target-URL` | Ja* | Upstream-Ziel (oder aus .env `QUEUE_TARGET_URL`) |
| `X-Bridge-Target-Key` | Ja* | API-Key (oder aus .env `QUEUE_TARGET_KEY`) |
| `X-Bridge-Queue-Mode` | Optional | Scheduler-Strategie (Default: `fifo`) |
| `X-Bridge-Translate` | Optional | `off` = Ollama-Übersetzung deaktivieren |

* Pflicht wenn `X-Bridge-Queue: on` gesetzt ist.

### .env Konfiguration der Bridge (optional, nur Defaults)

```env
# Queue-Modus per .env aktivieren (Optional - kann auch per Header)
# QUEUE_ENABLED=1

# Default-URL wenn nicht im Header gesetzt (Optional)
# QUEUE_TARGET_URL=https://api.cheapestinference.com/v1

# Default-Key wenn nicht im Header gesetzt (Optional - NICHT empfohlen)
# QUEUE_TARGET_KEY=sk-...

# Keep-alive Interval (Sekunden)
QUEUE_KEEPALIVE=15
```

### Verhalten

- **Keep-alive:** Solange ein Request in der Queue wartet, sendet die Bridge
  `: keepalive`-Frames an den Client. OpenCode sieht Fortschritt und löst
  keinen Retry aus.
- **Serialisierung:** Maximal 1 paralleler Upstream-Request. Alle weiteren
  warten in der Queue.
- **Rate-Limit:** Bei 429 mit "window opens" wird die API_URL+Key-Kombination
  für 60s blockiert. Die Original-Fehlermeldung wird bis dahin zurückgegeben.
- **Key-Sicherheit:** Der API-Key MUSS über den Header (`X-Bridge-Target-Key`)
  oder eine Umgebungsvariable ({env:...}) kommen — NICHT hardcoded in opencode.jsonc
  wenn das Repo geteilt wird.
- **Ziel-URL:** Kann pro Request per `X-Bridge-Target-URL` überschrieben
  werden (Multi-Target-Support) oder in .env als Default stehen.

### Wann was nutzen?

| Szenario | Lösung |
|---|---|
| Nur lokale Ollama | Variante "Ollama (bridged)" — kein Queue nötig |
| CheapestInference, 1 OpenCode-Fenster | Direkt zu CPI (ohne Bridge) |
| CheapestInference, mehrere parallele Anfragen | Bridge + Queue |
| Key nicht im Repo/Clients | Header `X-Bridge-Target-Key` oder {env:...} |
