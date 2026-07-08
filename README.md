# Autonomer Tool-Agent

Ein Agent, der eine Anfrage bekommt, **selbst entscheidet welche Tools er in
welcher Reihenfolge aufruft**, und autonom bis zum Ergebnis arbeitet.
Reasoning-Loop über die Anthropic Messages API.

Zwei strikt getrennte Phasen:

- **Phase 1 — `agent.py`**: Agent-Kern, Text rein / Text raus, Terminal.
  Läuft komplett eigenständig.
- **Phase 2 — `voice.py`**: Voice-Wrapper. Ruft nur `run_agent()` aus `agent.py`
  auf und ändert den Kern **nicht**.

---

## API-Key setzen (nicht im Code!)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # Linux / macOS
setx  ANTHROPIC_API_KEY "sk-ant-..."       # Windows (danach neues Terminal)
```

## Phase 1 starten

```bash
pip install anthropic
python agent.py
```

Beispiel-Session:

```
Du > Suche, welches das aktuelle Claude-Modell von Anthropic ist,
     und schreibe eine kurze Zusammenfassung in modell.txt.
```

---

## Die 3 Tools

| Tool          | Was es tut                                              | Sicherheit |
|---------------|---------------------------------------------------------|------------|
| `web_search`  | Websuche via DuckDuckGo (schlüssellos). Kein Netz/kein Treffer → **klar markierter Stub**, kein Crash. | read-only, extern |
| `file`        | `read` / `write` einer Textdatei **im Arbeitsordner**   | Pfad hart auf Arbeitsordner begrenzt, 1 MB Limit |
| `list_files`  | Listet den Arbeitsordner auf                            | read-only |

Der Arbeitsordner ist standardmäßig `./agent_workspace` (überschreibbar per
`AGENT_WORKDIR`). Er wird beim Start automatisch angelegt.

---

## Guardrails (im Code verankert, nicht nur im Prompt)

1. **Keine Dateien außerhalb des Arbeitsordners.** Jeder Pfad wird über
   `_safe_path()` aufgelöst und geprüft; `../`-Traversal, absolute Pfade und
   aus dem Ordner zeigende Symlinks werden mit `GuardrailError` abgelehnt.
2. **Keine Systembefehle / kein Shell / kein beliebiger Code.** Es gibt schlicht
   kein Tool dafür — der Agent kann nur die drei definierten Funktionen aufrufen.
3. **Größenlimit** von 1 MB pro Lese-/Schreibvorgang.
4. **Hartes Iterationslimit** von 8 Runden (`MAX_ITERATIONS`), danach sauberer
   Abbruch mit Meldung.
5. **Kein Crash bei Tool-Fehlern.** Jeder Tool-Aufruf ist gekapselt; Fehler und
   leere Ergebnisse gehen als `is_error`-Tool-Result zurück an das Modell, das
   dann einen anderen Weg wählen kann.

---

## Testfall (braucht ≥ 2 Tools nacheinander)

**Anfrage:**
> „Suche nach dem aktuellen Anthropic-Claude-Modell und schreibe eine
> 2-Satz-Zusammenfassung nach `modell.txt`. Liste danach den Ordner auf.“

**Erwarteter Ablauf:**

```
── Iteration 1 ──
  → Tool: web_search   Input: {"query": "aktuelles Anthropic Claude Modell"}
    ✓ [Websuche] Treffer: ...
── Iteration 2 ──
  → Tool: file         Input: {"action": "write", "path": "modell.txt", "content": "..."}
    ✓ [Datei] N Zeichen geschrieben nach modell.txt
── Iteration 3 ──
  → Tool: list_files   Input: {}
    ✓ [Liste] FILE modell.txt (N B)
── Iteration 4 ──
  (stop_reason = end_turn)
Agent > Ich habe ... in modell.txt gespeichert; der Ordner enthält modell.txt.
```

Das Modell ruft `web_search` → `file(write)` → `list_files` **selbst** in dieser
Reihenfolge auf; die Reihenfolge ist nicht fest verdrahtet.

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

- **STT**: `SpeechRecognition` mit `recognize_google` (kostenlos, ohne Key,
  braucht Internet). Offline-Alternative: `pip install openai-whisper`.
- **TTS**: `pyttsx3` (offline, ohne Key, plattformübergreifend).
- **Fehl-Erkennung**: Unverständliches/Rauschen (`UnknownValueError`, Timeout,
  Dienstfehler) wird abgefangen → der Agent wird gar nicht erst aufgerufen,
  stattdessen „Bitte wiederhole das“. Nach 3 Fehlversuchen Ende. Zusätzlich ein
  **Bestätigungs-Gate**: die verstandene Anfrage wird zurückgesprochen und muss
  mit „ja“ bestätigt werden, bevor der Agent (und damit Dateien/Tools) reagiert.

Der Voice-Layer importiert `run_agent` und ruft es unverändert auf — der
Agent-Kern weiß nichts von Sprache.

---

## Die 2 Stellen, an denen der Agent in der Praxis am ehesten bricht

1. **Websuche liefert Stub statt echter Antwort.**
   Die DuckDuckGo-Instant-Answer-API gibt für viele Anfragen nichts
   Strukturiertes zurück. Dann kommt ein `[Websuche – STUB]`-Text — der Agent
   arbeitet weiter, aber mit dünner Faktenlage, und schreibt evtl. eine
   inhaltlich schwache Zusammenfassung.
   *Woran du es merkst:* In der Terminal-Ausgabe steht `[Websuche – STUB]` oder
   `Suche nicht verfügbar`. **Fix:** in `tool_web_search()` einen echten
   Such-Dienst mit Key eintragen (Brave, Tavily, SerpAPI).

2. **Aufgabe passt nicht in 8 Iterationen.**
   Bei vage/mehrteilig formulierten Anfragen kann das Modell Tools „im Kreis“
   aufrufen und ins Iterationslimit laufen.
   *Woran du es merkst:* Die Antwort ist
   `[Abbruch] Iterationslimit (8) erreicht ...`. **Fix:** Anfrage konkreter
   stellen/aufteilen, oder `MAX_ITERATIONS` in `agent.py` erhöhen (Achtung:
   höhere Kosten und Latenz pro Anfrage).
