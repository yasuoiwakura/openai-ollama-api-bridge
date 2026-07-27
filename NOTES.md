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
