# Autonomer Recherche-Agent

Ein Agent, der eine Anfrage bekommt, **selbst entscheidet welche Tools er in
welcher Reihenfolge aufruft**, und autonom bis zum Ergebnis arbeitet — ausgelegt
darauf, sich **viel Information** zu beschaffen: Websuche liefert Quell-URLs,
`fetch_url` lädt ganze Seiten, mehrere Quellen werden abgeglichen. Läuft auf
**Claude Fable 5** (1M-Kontext) und hält damit große Mengen Recherche im Kopf.

Zwei strikt getrennte Phasen:

- **Phase 1 — `agent.py`**: Agent-Kern, Text rein / Text raus, Terminal.
- **Phase 2 — `voice.py`**: Voice-Wrapper. Ruft nur `run_agent()` auf und ändert
  den Kern **nicht**.

---

## API-Key setzen (nicht im Code!)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # Linux / macOS
setx  ANTHROPIC_API_KEY "sk-ant-..."       # Windows (danach neues Terminal)
```

> **Fable 5 braucht 30-Tage-Datenaufbewahrung.** Bei Organisationen mit
> Zero-Data-Retention schlägt jede Fable-5-Anfrage mit HTTP 400 fehl — dann in
> `agent.py` `MODEL = "claude-opus-4-8"` setzen.

## Phase 1 starten

```bash
pip install anthropic
python agent.py
```

---

## Die Tools (4)

| Tool          | Was es tut                                                        | Sicherheit |
|---------------|-------------------------------------------------------------------|------------|
| `web_search`  | Websuche via DuckDuckGo (schlüssellos), liefert Treffer **mit URL**. Kein Netz/kein Treffer → klar markierter Stub. | read-only, extern |
| `fetch_url`   | Lädt eine **ganze Webseite** und gibt den Text zurück (bis 40 000 Zeichen/Seite). Damit liest der Agent volle Artikel statt nur Snippets. | **SSRF-Schutz**: nur öffentliche http/https |
| `file`        | `read` / `write` einer Textdatei **im Arbeitsordner** (bis 2 MB)  | Pfad hart auf Arbeitsordner begrenzt |
| `list_files`  | Listet den Arbeitsordner auf                                      | read-only |

Der typische Recherche-Fluss: `web_search` (Quellen + URLs finden) →
`fetch_url` (die besten Seiten im Detail lesen, mehrfach) → ggf. `file` (viel
Material zwischenspeichern) → Antwort mit Quellen. Bis zu **15 Iterationen** und
`effort: high` geben dem Agenten Raum, viele Quellen abzuklappern.

Arbeitsordner: standardmäßig `./agent_workspace` (per `AGENT_WORKDIR`
überschreibbar), wird beim Start angelegt.

---

## Guardrails (im Code verankert, nicht nur im Prompt)

1. **Keine Dateien außerhalb des Arbeitsordners** — `_safe_path()` lehnt `../`,
   absolute Pfade und ausbrechende Symlinks ab.
2. **Kein SSRF** — `fetch_url` erlaubt nur öffentliche http/https-Hosts;
   `localhost`, `127.*`, private Netze (`10.*`, `192.168.*`, `172.16–31.*`),
   Link-Local/Cloud-Metadaten (`169.254.169.254`) und andere Schemata
   (`file://`, `ftp://`) werden geblockt — alle aufgelösten IPs werden geprüft.
3. **Größenlimits** — 2 MB pro Datei, 3 MB Download, 40 000 Zeichen/Seite ans Modell.
4. **Keine Systembefehle / kein Shell / kein beliebiger Code** — es gibt kein Tool dafür.
5. **Hartes Iterationslimit** von 15 Runden, danach sauberer Abbruch.
6. **Kein Crash bei Tool-Fehlern** — jeder Fehler geht als `is_error` zurück ans
   Modell, das dann einen anderen Weg wählen kann.

Zusätzlich: bei einer **Sicherheits-Ablehnung** durch Fable 5 übernimmt
automatisch das Fallback-Modell `claude-opus-4-8` (server-side fallback).

---

## Testfall (braucht ≥ 2 Tools nacheinander)

**Anfrage:**
> „Recherchiere, welche Modelle zur Claude-5-Familie gehören. Öffne mindestens
> zwei Quellen im Detail, schreibe eine Zusammenfassung mit Quellenangaben nach
> `claude5.md` und liste danach den Ordner auf.“

**Erwarteter Ablauf:**

```
── Iteration 1 ──
  → Tool: web_search   Input: {"query": "Claude 5 Familie Modelle Anthropic"}
    ✓ [Websuche] Treffer (URLs mit fetch_url öffnen): 1. ... <https://...>
── Iteration 2 ──
  → Tool: fetch_url    Input: {"url": "https://..."}
    ✓ [Fetch] https://...  <voller Seitentext>
── Iteration 3 ──
  → Tool: fetch_url    Input: {"url": "https://..."}   (zweite Quelle)
    ✓ [Fetch] ...
── Iteration 4 ──
  → Tool: file         Input: {"action": "write", "path": "claude5.md", ...}
    ✓ [Datei] N Zeichen geschrieben nach claude5.md
── Iteration 5 ──
  → Tool: list_files   Input: {}
    ✓ [Liste] FILE claude5.md (N B)
── Iteration 6 ──
  (stop_reason = end_turn)
Agent > Zusammenfassung … Quellen: … (gespeichert in claude5.md).
```

Reihenfolge und Anzahl der Aufrufe wählt das Modell **selbst**.

---

## Phase 2 — Voice

```bash
pip install SpeechRecognition pyttsx3
# Mikrofon-Backend:
#   Linux:   sudo apt install portaudio19-dev && pip install pyaudio
#   macOS:   brew install portaudio && pip install pyaudio
#   Windows: pip install pyaudio
python voice.py
```

- **STT**: `SpeechRecognition` + `recognize_google` (kostenlos, ohne Key,
  braucht Internet). Offline-Alternative: `pip install openai-whisper`.
- **TTS**: `pyttsx3` (offline, ohne Key).
- **Fehl-Erkennung**: Unverständliches/Rauschen wird abgefangen → der Agent
  wird gar nicht erst aufgerufen. Nach 3 Fehlversuchen Ende. Zusätzlich ein
  **Bestätigungs-Gate**: die verstandene Anfrage wird zurückgesprochen und muss
  mit „ja“ bestätigt werden, bevor der Agent reagiert.

Der Voice-Layer importiert `run_agent` und ruft es unverändert auf.

---

## Die 2 Stellen, an denen der Agent in der Praxis am ehesten bricht

1. **Websuche liefert Stub statt echter Treffer.**
   Die schlüssellose DuckDuckGo-API gibt für viele Anfragen nichts
   Strukturiertes zurück — dann fehlen dem Agenten die URLs zum Nachladen, und
   `fetch_url` läuft ins Leere.
   *Woran du es merkst:* `[Websuche – STUB]` in der Ausgabe. **Fix:** in
   `tool_web_search()` einen echten Such-Dienst mit Key eintragen (Brave,
   Tavily, SerpAPI) — dann bekommt der Agent verlässlich viele Quell-URLs.

2. **Viel Recherche → Iterationslimit oder große Kontextmenge.**
   Bei breiten Anfragen ruft Fable 5 gern viele `fetch_url` hintereinander auf.
   Das kann ins 15er-Limit laufen ODER (bei sehr vielen großen Seiten) Kosten/
   Latenz stark erhöhen.
   *Woran du es merkst:* `[Abbruch] Iterationslimit (15) erreicht …`, oder
   spürbar langsame/teure Läufe. **Fix:** `MAX_ITERATIONS`, `MAX_FETCH_CHARS`
   und `MAX_SEARCH_RESULTS` in `agent.py` an dein Budget anpassen; Anfragen
   fokussierter stellen.
