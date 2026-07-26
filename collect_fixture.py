#!/usr/bin/env python3
"""Sendet 01_req_completions → /v1/chat/completions (speichert als 04)
   und      02_req_chat      → /api/chat             (speichert als 03)."""
import json, sys, os, requests
S_OLLAMA = "http://192.168.0.42:18080" # original: S_OLLAMA = "http://192.168.0.42:11434"
OLLAMA = sys.argv[1] if len(sys.argv) > 1 else S_OLLAMA
DIR = "testdata/fixtures/opencode_plan_mode"

def load(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as f:
        return json.load(f)

def save(name, data):
    with open(os.path.join(DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def collect(body, url, outfile, raw_key):
    r = requests.post(url, json=body, stream=body.get("stream", False))
    if body.get("stream"):
        lines = []
        for line in r.iter_lines():
            if line:
                lines.append(line.decode("utf-8") if isinstance(line, bytes) else line)
        save(outfile, {"_raw": "\n".join(lines)})
    else:
        save(outfile, r.json())
    print(f"  {outfile}  Status {r.status_code}")

print(f"Ollama: {OLLAMA}")
collect(load("01_req_completions.json"), f"{OLLAMA}/v1/chat/completions", "04_resp_completions.json", "_raw_sse")
collect(load("02_req_chat.json"),      f"{OLLAMA}/api/chat",             "03_resp_chat.json",        "_raw_ndjson")
