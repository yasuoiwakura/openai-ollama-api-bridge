# Changelog

Alle nennenswerten Änderungen an `py-ollama-openai-bridge`. Datumsbasiert (`YYYY-MM-DD`), angelehnt an [Keep a Changelog](https://keepachangelog.com/). Tag = `git log --date=short`. Unveröffentlichtes oben.

Format: `Added` / `Changed` / `Fixed` / `Removed` / `Docs`. Verweise auf `proxy.py:line`, `.env.example:line`, `compose.yml:line`, Commit-Hash.

---

## [Unreleased]

### Known Issue (noch offen, hier dokumentiert – kein Code-Fix in diesem Commit)
- **Queue-Hang nach HTTP 524 / stalled Upstream** – Worst-Case aus Logs `2026-09-02 11:11` / `2026-09-03 12:38`: `submit entry=... pos=...` → `POST -> 200` → `upstream error: HTTP 524: <!DOCTYPE html>` → `post-200 error (status=524), sent as SSE event, closing` → `entry=... start` → nächster `submit ...` bleibt auf `hold`, alle Requests hängen, manueller Kill nötig. Code-Stand laut `proxy.py:897`/`proxy.py:810`: `_QueueWorker._process()` blockiert in `requests.post(..., stream=True)` / `_iter_sse_events()` (`proxy.py:810` hat noch `finally: if buf: yield buf + b"\n\n"` → `RuntimeError: generator ignored GeneratorExit` `proxy.py:747` bei `cancel`), `timeout` (`proxy.py:944` `QUEUE_STREAM_TIMEOUT=600`) gilt nur pro `socket.recv()`. Kein Cancel-Watcher vorhanden – `proxy.py:1036` prüft `entry.cancel` nur zwischen Events, steckendes `iter_lines()` wird nicht unterbrochen. `entry.result.put(None)` ohne Timeout (`proxy.py:1000`/`1058`) kann bei `queue.Full` hängen. Siehe Plan `.opencode/plans/queue-fix.md` – Fix noch offen.
- `RuntimeError: generator ignored GeneratorExit` in `_iter_sse_events` (`proxy.py:810` `finally`) – noch vorhanden, nicht behoben.

### Changed (Ist-Stand im Code, hier korrekt dokumentiert)
- **Queue – Abbruchbedingung bei niedrigem Durchsatz entfernt.** Eine zuvor implementierte Abbruchbedingung wurde zunächst auf `<1 TPS` reduziert und dann vollständig deaktiviert. Ein Anbieter, der nur 1 Verbindung gleichzeitig zulässt, liefert nicht regulär, aber häufig `<1 TPS`. Ein Abbruch und Neuaufbau der Verbindung wurde dabei als Verstoß gegen diese Anfragenbegrenzung gewertet. Daher wird eine Verbindung, die lange nur kleine Datenmengen erhält, aktuell nicht automatisch beendet; stattdessen gibt es im Log die Meldungen `STALL` (`!!!` keine Daten seit 5/10/20s `proxy.py:399`), `SLOW` (`~~~` `0 < TPS < UPSTREAM_LOW_TPS` `proxy.py:414`) und `TIMING` (`UPSTREAM done` `proxy.py:1067`). Im Code umgesetzt via `UpstreamStallWatcher` (`proxy.py:304`) mit `watcher.record(len(ev))` (`proxy.py:1039`) + sofortigem `entry.result.put(ev, timeout=2)` downstream, Queue bleibt seriell mit `UPSTREAM_PAUSE=1.5` (`proxy.py:603`). Schwelle `UPSTREAM_LOW_TPS` (`compose.yml:12`, `.env.example:69`, Default `1.0` `proxy.py:619`) wirkt nur als Logging-Schwelle. **TODO:** Die Abbruchbedingung wird zu einem späteren Zeitpunkt als parametrierbare Option wiedereingeführt.

---

## 2026-09-10

### Changed
- `eb5a24c` – Queue-Default-Prio `999` → `100` (`proxy.py:1047`, `1384`). Höhere Prios (1 = höchste) greifen früher; beeinflusst `_QueueScheduler._next_by_priority()` (`proxy.py:578`).

## 2026-09-08

### Fixed
- `02b62fd` – Verbleibende Pause-Nachricht korrigiert (Logging).

## 2026-09-07

### Added
- `UPSTREAM_PAUSE=1.5` (`compose.yml:11`, `.env.example:65`, `proxy.py`-Env) – feste Pause zwischen Ende und Start des nächsten Upstream-Requests. Verhindert `cheapestinference.com` Throttling/Timeouts. Override via `.env:65`.
- `UPSTREAM_LOW_TPS=1.0` (`compose.yml:12`, `.env.example:69`) – Schwelle für Downgraded-Erkennung (SSE-Events/s). Darunter gilt Upstream als gedrosselt, nicht als `STALL`; Verbindung wird nicht abgebrochen.
- STALL/DEBUG-Logging (`0aa1cef`, `b03e1b5`, `110edd6`) – `STALL`-Detektion + `DEBUG TIMING` Ausgaben für Queue-Timing.

### Fixed
- `f09e01a` `3ad50f1` – **Legacy Hotfix**: 1,5s `sleep` zwischen Requests + `429 Retry-After` von Cloudflare respektieren. Reduziert `HTTP 429` / `524` Kaskaden.

## 2026-09-03

### Changed
- `7107a6a` – 1st draft Queue-Stabilität (Vorarbeit zu `3a46919`).

### Fixed
- Zusammenhang mit `2026-09-02`: teilweiser Fix für toten Queue-Zustand, aber 524-Fall blieb reproduzierbar (siehe Unreleased).

## 2026-09-02

### Fixed
- `3a46919` – `fix dead queue upon error 500` – erster Versuch, Queue nach `HTTP 500` wieder freizugeben. Log vorher: `entry ... upstream error` → Queue blieb leer laufend.

## 2026-08-31

### Added
- `955e22e` – `PLAN13 config schema` (`config.schema.yaml`, `IMPLEMENTATION_PLAN.md:1`) – Entwurf für YAML/JSON config-gesteuerte Bridge (mehrere AI-Gruppen, Endpoints, Queue/Translate pro Gruppe, Env-Substitution).

## 2026-08-30

### Added
- `4f1d3d1` – Server-Auswahl via Modell-Parameter `model[server=...]` (`proxy.py:336`, `README.md:368`). Werte `ollama`/`1` → Primary, `failover`/`2` → Secondary, numerisch → Index in `_all_targets`. Per `README.md:390` in Queue-Mode ignoriert (Target via `X-Bridge-Target-URL`).
- `78386ce` – Auto-Pull via `[pull=true]` im Modellnamen (`proxy.py:1122`, `README.md:10`). Bei `404 model not found` → `POST /api/pull` (`proxy.py:264`) mit Retry.
- `d29938b` `ece58cd` `cdd6ea6` – `404 ollama pull error` Handling + hilfreichere Downstream-Meldung `ollama pull <model>` + Doku (`doc/feat/*`).

### Changed
- `de16b55` / `cdd6ea6` – Fehlermeldung bei fehlendem Modell vereinheitlicht.

## 2026-08-28

### Fixed
- `b751f99` – `fix 413 gzip issue` – `resp.raw.read(MAX_ERROR_BODY)` + `gzip`/`deflate` Dekodierung (`proxy.py:701-713`) beim direkten Forward ohne Manipulation. Verhindert kaputte 413-Bodies.

## 2026-08-27

### Added
- `a6393bc` – **RateLimitTracker** 60s Block bei `429` mit `"window opens"` (`proxy.py:166-188`, `759-763`, `1372-1381`). `RateLimitTracker.block()` sperrt `(api_url, api_key_hash)` 60s, `get_error()` gibt gecachten Body als `429` zurück.
- `9b0ec6f` `b058f68` `8c4c683` – `QUEUE_FALLBACK_TIMEOUT` (Default 15s, `proxy.py:432`, `1322`) – wartet vor `200 OK` an Client, nutzt echten Upstream-Status falls früh verfügbar. Verhindert Rate-Spiele; via `X-Bridge-Fallback-Timeout` überschreibbar. + `BRIDGE_SIZE_TRACKER` global (`proxy.py:434`).
- `8c4c683` `1698b40` – **SizeTracker** (`proxy.py:67-145`) – lernt bei `413` kleinste Body-Größe pro `(api_url, key_hash)`, blockt via `is_too_large()` (`proxy.py:1361`). Nur `SIZE_413_ERROR_TYPES` (`proxy.py:149`) werden gelernt, `unknown_413` wird 1:1 durchgereicht (`proxy.py:744`). `X-Bridge-Size-Tracker` Header toggle.
- `2df22b5` `6dab2fb` `0eda534` `7783d9d` – `HIDE_HEALTHCHECK` (`proxy.py:425`) + `get_bool_env()` (`proxy.py:58`).
- `e36878d` `2c026b0` `afee018` – **Modelname-Parameter** `_parse_model_name()` (`proxy.py:193`) Syntax `model[key=val;...]` (`README.md:355`). Unterstützt `num_ctx`, `temperature`, `Prio`, `session-id`, `server`, `pull`.
- `b914c67` `9bab180` `da0e9f2` `b63910d` `60604a4` – `compose.yml:1` Erweiterungen: Ports, `web_password`, `--set`, `BRIDGE_PORT` Env, mitmproxy Services `mitmproxy-bridge-6081-6082` / `mitmproxy-cpi-6083-6084`.
- `37cf6e4` `65b60f8` – `doc/features/feat-006-plain-queue.md:1` erster Queue-Entwurf (später `queue_mode.py` Prototyp).
- Diverse `doc/feat/01-09` Scaffolds für kommende `app/` config-driven Bridge.

### Changed
- `227e07c` – Queue: immer JSON senden, vereinheitlicht Content-Type.
- `b058f68` – Queue-Workflow: nicht sofort `200` senden, sondern Fallback-Fenster abwarten (Vorläufer zu `QUEUE_FALLBACK_TIMEOUT`).
- `754aa5d` – Queue: Body-Re-Serialisierung ohne EOL-Ersetzung, verhindert `max context` Treffer.
- `e36878d` – Body-Größe via `len(raw_body)` vs `original_payload` getrennt tracken (`proxy.py:1394`).

### Fixed
- `d53c173` `1698b40` `b751f99` – mehrere kritische 413/500 Pfade (siehe oben).

## 2026-08-27 (früh) – 2026-07-28

### Added
- Queue Grundgerüst (`QUEUE_ENABLED`, `QUEUE_TARGET_URL/KEY`, `QUEUE_MODE_DEFAULT=fifo`, `QUEUE_KEEPALIVE=15`, `QUEUE_CONNECT_TIMEOUT=60`, `QUEUE_STREAM_TIMEOUT=600`, `QUEUE_MAX_SIZE=500`, `QUEUE_MAX_WAITTIME=300` – `proxy.py:419-436`) – serialisiert N parallele Requests auf 1 Upstream (`NOTES.md:84`, `README.md:5`). Scheduler `fifo/session/prio` (`proxy.py:506-608`), `_QueueWorker` (`proxy.py:627`).
- Failover-Routing (`OLLAMA_URL` → `FAILOVER_OLLAMA_URL` mit `FAILOVER_NUM_CTX/PREDICT/KEEP_ALIVE/TIMEOUT` – `proxy.py:296-363`, `README.md:424`) + Healthcheck `GET /api/tags` (`proxy.py:942`).

### Docs
- `SPEC.md:1`, `README.md:1`, `NOTES.md:1`, `testdata/MAPPING.md:1` Erstfassungen.

## 2026-07-28 – 2026-07-26

### Added
- Initiale Bridge: `proxy.py:1` OpenAI `/v1/chat/completions` → Ollama `/api/chat` via `translator.py:1`, Streaming SSE `translate_response`, `ThreadingHTTPServer` (`proxy.py:239`), `load_env()` (`.env`), `BRIDGE_HOST/PORT` (`proxy.py:379`), `NUM_CTX`, `KEEP_ALIVE`, `HEADER_OVERRULES` (`proxy.py:394`).
- Docker (`Dockerfile`, `compose.yml:1`), `requirements.txt:1` `requests>=2.32`, `mitmproxy` Logging-Layer (`SPEC.md:137`), Mermaid-Diagramme (`README.md:44`, `doc/`), `IMPLEMENTATION_PLAN.md:1`.
- Logging `BrokenPipeError` / `ConnectionResetError` (`proxy.py:847`, `1540`), zentraler Exception-Handler (`110e136`), `flush` für Start-Message (`2c1a6e6`).

### Changed
- `f17d90c` `b1c20c3` `8024c89` – README gestrafft + Mermaid ergänzt.
- `1e59579` – HomeLab Beispiel-Setup (`README.md:522`).

---

## Hinweise für README-Nutzer (fehlende Doku vor Changelog)

`README.md:331` `All options` listet nur `NUM_CTX` etc., nicht die Queue-Envs (`QUEUE_*`, `UPSTREAM_PAUSE`, `UPSTREAM_LOW_TPS`, `BRIDGE_SIZE_TRACKER`, `HIDE_HEALTHCHECK`). `NOTES.md:219` Header-Tabelle kennt `X-Bridge-Queue`, `X-Bridge-Target-URL/Key`, `X-Bridge-Queue-Mode`, `X-Bridge-Translate`, aber alte Namen `X-Bridge-Mode`/`X-Upstream-Key` aus `doc/features/feat-006-plain-queue.md:64` sind obsolet. Für korrekte `opencode.jsonc` siehe `NOTES.md:128` (Queue) + diesen Changelog.

