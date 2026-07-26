# Template & Notes

## # opencode.json 
### Basis-Template für ollama über den Proxy

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

### Parameter-Erklärung

| Feld | Wert | Wirkung |
|---|---|---|
| `tool_call: true` | Pflicht | Ohne kein Tool-Support |
| `attachment: true` | Pflicht | Sonst sind Dateianhänge blockiert |
| `reasoning: true` | Optional | Thinking/Reasoning UI, schadet nicht |
| `temperature: true` | Pflicht | Sonst wird Temperatur aus Agent-Config ignoriert |
| `modalities.input` | `["text"]` | Für Text-Modelle. **Nur** auf `["text", "image"]` setzen wenn das Modell wirklich Bilder kann (z.B. LLaVA, Qwen2.5-VL) |
| `limit.context` | 64000 | Muss mit `DEFAULT_NUM_CTX` im Proxy übereinstimmen (dort 64K). Steuert wann OpenCode kompaktiert |
| `limit.output` | 40960 | Schema-Pflicht. OpenCode cappt intern auf 32K, Proxy überschreibt via `DEFAULT_NUM_PREDICT` |

## Fallstricke

- **`limit.input` NIEMALS setzen** – überschreibt `limit.context` und verzögert Compaction. Modell bekommt dann mehr Tokens als sein Context Window (`num_ctx`) erlaubt
- **`limit.context` muss <= `DEFAULT_NUM_CTX`** (Proxy `.env`) sein, sonst compacted OpenCode zu spät
- **`name`-Feld** dient nur der Sortierung/Anzeige in der Modell-Auswahl. Mit `_`-Prefix landen Modelle oben
- **`$schema`** aktiviert Auto-Vervollständigung im Editor

## VRAM-Faustregel

| Context (num_ctx) | Qwen3.5:9b | gpt-oss:20b |
|---|---|---|
| 32K | ~8 GB | ~10 GB |
| 64K | ~10 GB | ~14 GB |
| 128K | ~16 GB | ❌ nicht möglich |
