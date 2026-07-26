#!/usr/bin/env python3
"""
Übersetzt zwischen OpenAI Completions API (01/04) und Ollama Chat API (02/03).

Beide Richtungen:
  translate_request(openai_req)           → ollama_req     (01 → 02)
  translate_response(ollama_chunks)        → yields sse_dict (03 → 04)

Die Funktionen arbeiten auf Python-Dicts/-Iterables und sind daher
sowohl für Test-Files als auch für Live-Traffic verwendbar.

Siehe testdata/MAPPING.md für das vollständige Mapping.
"""
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from collections.abc import Iterable, Generator

DEFAULT_CONFIG = {
    "num_ctx": 32768,
    "keep_alive": "30m",
}

# ── Hilfsfunktionen ──────────────────────────────────────────────

def _flatten_content(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return "\n".join(
            p.get("text", "") for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return c if isinstance(c, str) else ""


def _extract_images(msg: dict) -> list[str] | None:
    """Extrahiert base64-images aus OpenAI content-array → list of base64 strings."""
    c = msg.get("content")
    if not isinstance(c, list):
        return None
    images = []
    for p in c:
        if isinstance(p, dict) and p.get("type") == "image_url":
            url = p.get("image_url", {}).get("url", "")
            if url.startswith("data:image/"):
                base64 = url.split(",", 1)[-1] if "," in url else url
                images.append(base64)
    return images if images else None


def _convert_tool_call_arguments(tc: dict, direction: str) -> dict:
    """Wandelt tool_calls[].function.arguments zwischen string und object.

    direction="to_ollama":  json.loads(string) → object
    direction="to_openai":  json.dumps(object) → string
    """
    tc = deepcopy(tc)
    fn = tc.get("function", {})
    args = fn.get("arguments")
    if args is not None:
        if direction == "to_ollama" and isinstance(args, str):
            fn["arguments"] = json.loads(args)
        elif direction == "to_openai" and not isinstance(args, str):
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
    return tc


def _iso_to_unix(iso_str: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return int(datetime.now(timezone.utc).timestamp())


# ── Request: 01 → 02 ────────────────────────────────────────────

def translate_request(openai_req: dict, config: dict | None = None) -> dict:
    """OpenAI /v1/chat/completions Request → Ollama /api/chat Request."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    # Lookup: tool_call_id → function_name (für tool-Callbacks)
    call_id_to_name: dict[str, str] = {}

    messages = []
    for m in openai_req.get("messages", []):
        role = m["role"]

        # Rollen-Mapping
        if role == "developer":
            role = "system"
        elif role == "function":
            role = "tool"

        nm = {"role": role}

        if "content" in m:
            nm["content"] = _flatten_content(m)

        # Bilder aus content-array extrahieren (Ollama: images-Feld)
        images = _extract_images(m)
        if images:
            nm["images"] = images

        # Tool-Calls vom assistant: arguments string → object, IDs merken
        if "tool_calls" in m:
            ollama_tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                call_id_to_name[tc.get("id", "")] = fn.get("name", "")
                ollama_tcs.append(_convert_tool_call_arguments(tc, "to_ollama"))
            nm["tool_calls"] = ollama_tcs

        # Tool-Callback: tool_call_id → tool_name
        if role == "tool" and "tool_call_id" in m:
            tid = m["tool_call_id"]
            nm["tool_name"] = call_id_to_name.get(tid, tid)

        messages.append(nm)

    ollama = {
        "model": openai_req["model"],
        "messages": messages,
        "stream": openai_req.get("stream", True),
        "options": {
            "num_predict": cfg.get("num_predict") or openai_req.get("max_tokens", 32000),
            "num_ctx": cfg["num_ctx"],
        },
        "keep_alive": cfg["keep_alive"],
    }

    # Config-Defaults aus .env in options (werden von openai_req überschrieben)
    for key in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
                "frequency_penalty", "presence_penalty", "seed", "repeat_last_n", "stop"):
        if key in cfg:
            ollama["options"][key] = cfg[key]

    if "tools" in openai_req and openai_req["tools"]:
        ollama["tools"] = deepcopy(openai_req["tools"])
    if "tool_choice" in openai_req:
        ollama["tool_choice"] = openai_req["tool_choice"]
    if "temperature" in openai_req:
        ollama["options"]["temperature"] = openai_req["temperature"]
    if "top_p" in openai_req:
        ollama["options"]["top_p"] = openai_req["top_p"]
    if "presence_penalty" in openai_req:
        ollama["options"]["repeat_penalty"] = 1.0 + openai_req["presence_penalty"]
    if "frequency_penalty" in openai_req:
        ollama["options"]["frequency_penalty"] = openai_req["frequency_penalty"]
    if "seed" in openai_req:
        ollama["options"]["seed"] = openai_req["seed"]
    if "stop" in openai_req:
        ollama["options"]["stop"] = openai_req["stop"]

    # response_format ↔ format
    rf = openai_req.get("response_format")
    if rf:
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            ollama["format"] = "json"
        elif isinstance(rf, dict) and rf.get("type") == "json_schema":
            ollama["format"] = rf.get("json_schema", rf)

    return ollama


# ── Response: 03 → 04 ───────────────────────────────────────────

def translate_response(ollama_chunks: Iterable[dict]) -> Generator[dict, None, None]:
    """
    Ollama Chat Response (NDJSON-Objekte) → OpenAI SSE-Events (Dicts).

    Yields Dicts, die als `data: {json.dumps(dict)}\n\n` formatiert werden können.
    Das letzte Event ist `None` (entspricht `data: [DONE]`).
    """
    chat_id = None
    model = None
    chunk_index = 0
    saw_tool_calls = False

    for chunk in ollama_chunks:
        if not isinstance(chunk, dict):
            continue

        model = chunk.get("model", model)
        created = _iso_to_unix(chunk.get("created_at", ""))

        if chat_id is None:
            chat_id = f"chatcmpl-{chunk_index}"

        msg = chunk.get("message", {})
        role = msg.get("role", "assistant")
        thinking = msg.get("thinking", "")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")

        done = chunk.get("done", False)
        done_reason = chunk.get("done_reason") if done else None

        # "load"/"unload" sind interne Ollama-Events, keine Antwort
        if done and done_reason in ("load", "unload"):
            yield None
            return

        # Delta aufbauen
        delta = {"role": role} if chunk_index == 0 else {}

        if thinking:
            delta["reasoning"] = thinking
            if not content:
                delta["content"] = thinking
        if content:
            delta["content"] = content

        if tool_calls:
            saw_tool_calls = True
            ollama_tc = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                ollama_tc.append({
                    "id": tc.get("id", f"call_{chunk_index}"),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": json.dumps(fn.get("arguments", {}), ensure_ascii=False),
                    },
                })
            delta["tool_calls"] = ollama_tc

        finish_reason = None
        if done:
            if saw_tool_calls:
                finish_reason = "tool_calls"
            elif done_reason in ("stop", "length", "content_filter"):
                finish_reason = done_reason
            else:
                finish_reason = "stop"

        if delta or done:
            yield {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "system_fingerprint": "fp_ollama",
                "choices": [{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }],
            }
            chunk_index += 1

        if done:
            # Usage-Chunk
            usage = {}
            if chunk.get("prompt_eval_count") is not None:
                usage["prompt_tokens"] = chunk["prompt_eval_count"]
            if chunk.get("eval_count") is not None:
                usage["completion_tokens"] = chunk["eval_count"]
            if usage:
                usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                yield {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "system_fingerprint": "fp_ollama",
                    "choices": [],
                    "usage": usage,
                }

            # [DONE]-Sentinel – danach Generator stoppen,
            # sonst blockiert iter_lines() auf die nächste Zeile.
            yield None
            return


# ── Format-Helper für Test-Files ─────────────────────────────────

def parse_ndjson(text: str) -> list[dict]:
    """Einzelsring mit newline-getrennten JSON-Objekten → Liste."""
    return [json.loads(line) for line in text.strip().split("\n") if line.strip()]


def format_ndjson(objs: list[dict]) -> str:
    """Liste von Dicts → NDJSON-String."""
    return "\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n"


def parse_sse(text: str) -> list[dict]:
    """SSE-Text (`data: {...}\n\n`) → Liste von Dicts (ohne [DONE])."""
    result = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: ") and not line.startswith("data: [DONE]"):
            result.append(json.loads(line[6:]))
    return result


def format_sse(events: list[dict | None]) -> str:
    """Liste von SSE-Dicts (None = [DONE]) → SSE-String."""
    lines = []
    for ev in events:
        if ev is None:
            lines.append("data: [DONE]")
        else:
            lines.append(f"data: {json.dumps(ev, ensure_ascii=False)}")
    return "\n\n".join(lines) + "\n\n"


# ── CLI: Test-File-Mapping ──────────────────────────────────────

def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def run_fixture(scenario_dir: str, config: dict | None = None):
    """
    Führt die komplette Fixture-Übersetzung für ein Szenario aus:

      01_req_completions.json  → translate_request  → 02_req_chat.json
      03_resp_chat.json          → translate_response → 04_resp_completions.json
    """
    import os

    p = lambda name: os.path.join(scenario_dir, name)

    # ── Request (01 → 02) ────────────────────────────────────────
    if os.path.exists(p("01_req_completions.json")):
        req = _load_json(p("01_req_completions.json"))
        ollama_req = translate_request(req, config)
        _save_json(p("02_req_chat.json"), ollama_req)
        print(f"  Erzeugt: {p('02_req_chat.json')}")

    # ── Response (03 → 04) ───────────────────────────────────────
    if os.path.exists(p("03_resp_chat.json")):
        raw = _load_json(p("03_resp_chat.json"))
        raw_text = raw.get("_raw", "")
        if raw_text:
            chunks = parse_ndjson(raw_text)
            events = list(translate_response(chunks))
            out = {"_raw": format_sse(events)}
            _save_json(p("04_resp_completions.json"), out)
            print(f"  Erzeugt: {p('04_resp_completions.json')}  ({len(events)} Events)")


if __name__ == "__main__":
    import sys
    scenario = sys.argv[1] if len(sys.argv) > 1 else "testdata/fixtures/opencode_plan_mode"
    print(f"Fixture-Mapping: {scenario}")
    run_fixture(scenario)
