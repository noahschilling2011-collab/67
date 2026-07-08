"""
Phase 1 — Autonomer Tool-Agent (Text rein / Text raus, Terminal).

Ein Agent, der eine Anfrage bekommt, selbst entscheidet welche Tools er in
welcher Reihenfolge aufruft, und autonom bis zum Ergebnis arbeitet.

Reasoning-Loop: Anthropic Messages API (manuelle Kontrollschleife, damit das
harte Iterationslimit und die Guardrails vollständig im Code liegen).

KEIN API-Key im Code. Setze ihn als Umgebungsvariable:
    export ANTHROPIC_API_KEY="sk-ant-..."      # Linux / macOS
    setx  ANTHROPIC_API_KEY "sk-ant-..."       # Windows (neues Terminal öffnen)

Start:
    pip install anthropic
    python agent.py
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

MODEL = "claude-opus-4-8"        # aktuelles Opus-Modell
MAX_TOKENS = 4096
MAX_ITERATIONS = 8               # HARTES LIMIT — danach sauberer Abbruch
MAX_FILE_BYTES = 1_000_000       # 1 MB Lese-/Schreib-Obergrenze pro Datei

# Arbeitsordner: alles läuft ausschließlich hier drin. Wird beim Start angelegt.
WORK_DIR = Path(os.environ.get("AGENT_WORKDIR", "./agent_workspace")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Guardrails
# ─────────────────────────────────────────────────────────────────────────────
#
# Was der Agent NIE tut (im Code verankert, nicht nur im System-Prompt):
#   1. Dateien außerhalb von WORK_DIR lesen/schreiben  -> _safe_path()
#      Blockiert absolute Ausbrüche, "../"-Traversal und Symlinks, die
#      aus dem Arbeitsordner herauszeigen.
#   2. Dateien über MAX_FILE_BYTES lesen/schreiben     -> Größen-Check
#   3. Systembefehle / Shell / beliebiger Python-Code   -> es gibt schlicht
#      kein Tool dafür. Der Agent kann nur die unten definierten Funktionen
#      aufrufen; alles andere ist nicht erreichbar.
#   4. Mehr als MAX_ITERATIONS Runden                   -> Kontrollschleife
#
# Sicherheitsgrenze: Tool-Eingaben sind Modell-Output und damit nicht
# vertrauenswürdig. Jede Datei-Operation wird deshalb hart validiert.


class GuardrailError(Exception):
    """Wird ausgelöst, wenn eine Aktion eine Sicherheitsgrenze verletzt."""


def _safe_path(raw_path: str) -> Path:
    """Löst einen vom Modell gelieferten Pfad relativ zu WORK_DIR auf und
    stellt sicher, dass er WORK_DIR nicht verlässt. Sonst GuardrailError."""
    if not raw_path or not isinstance(raw_path, str):
        raise GuardrailError("Leerer oder ungültiger Pfad.")

    candidate = (WORK_DIR / raw_path).resolve()
    # is_relative_to gibt es ab Python 3.9. Prüft, ob candidate in WORK_DIR liegt.
    if candidate != WORK_DIR and WORK_DIR not in candidate.parents:
        raise GuardrailError(
            f"Zugriff verweigert: '{raw_path}' liegt außerhalb des Arbeitsordners."
        )
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Websuche
# ─────────────────────────────────────────────────────────────────────────────
#
# Nutzt die schlüssellose DuckDuckGo Instant-Answer-API. Wenn kein Netz da ist
# oder die API nichts liefert, gibt es ein KLAR MARKIERTES Stub-Ergebnis zurück
# (kein Crash). Für eine echte Volltextsuche hier eine API mit Key eintragen
# (z. B. Brave Search, Tavily, SerpAPI) — Rückgabe bleibt ein String.

def tool_web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "[Websuche] Leere Suchanfrage."

    try:
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "no_html": "1", "no_redirect": "1"}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "tool-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        parts: list[str] = []
        if data.get("AbstractText"):
            src = data.get("AbstractSource", "")
            parts.append(f"{data['AbstractText']} (Quelle: {src})")

        for topic in data.get("RelatedTopics", []):
            text = topic.get("Text") if isinstance(topic, dict) else None
            if text:
                parts.append(text)
            if len(parts) >= 5:
                break

        if parts:
            return "[Websuche] Treffer:\n- " + "\n- ".join(parts[:5])

        # API erreichbar, aber ohne verwertbares Ergebnis -> klarer Stub.
        return (
            f"[Websuche – STUB] Keine strukturierte Antwort für '{query}'. "
            "Für echte Suche einen API-Key-Dienst in tool_web_search() eintragen."
        )
    except Exception as exc:  # Netz weg, Timeout, JSON kaputt ...
        return (
            f"[Websuche – STUB] Suche nicht verfügbar ({type(exc).__name__}): {exc}. "
            f"Angefragt war: '{query}'."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — Datei lesen / schreiben (im Arbeitsordner)
# ─────────────────────────────────────────────────────────────────────────────

def tool_file(action: str, path: str, content: str | None = None) -> str:
    action = (action or "").lower().strip()
    target = _safe_path(path)  # Guardrail: bleibt garantiert in WORK_DIR

    if action == "read":
        if not target.exists() or not target.is_file():
            return f"[Datei] Nicht gefunden: {path}"
        if target.stat().st_size > MAX_FILE_BYTES:
            return f"[Datei] Zu groß (> {MAX_FILE_BYTES} Bytes): {path}"
        return target.read_text(encoding="utf-8", errors="replace")

    if action == "write":
        text = content or ""
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise GuardrailError(f"Schreibinhalt zu groß (> {MAX_FILE_BYTES} Bytes).")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return f"[Datei] {len(text)} Zeichen geschrieben nach {path}"

    return f"[Datei] Unbekannte Aktion '{action}'. Erlaubt: 'read', 'write'."


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — Dateien im Arbeitsordner auflisten
# ─────────────────────────────────────────────────────────────────────────────
#
# Passt zum Ziel (Datei-Recherche/-Zusammenfassung): der Agent kann sehen,
# was schon da ist, bevor er liest oder schreibt. Read-only, kann nichts kaputt
# machen — und lässt sich im Testfall gut als dritter Schritt einhängen.

def tool_list_files(subdir: str = "") -> str:
    base = _safe_path(subdir) if subdir else WORK_DIR
    if not base.exists():
        return f"[Liste] Ordner existiert nicht: {subdir or '.'}"
    entries = sorted(base.iterdir())
    if not entries:
        return "[Liste] Arbeitsordner ist leer."
    lines = []
    for e in entries:
        kind = "DIR " if e.is_dir() else "FILE"
        size = e.stat().st_size if e.is_file() else 0
        lines.append(f"{kind} {e.name} ({size} B)")
    return "[Liste]\n" + "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Tool-Registry: JSON-Schema (für Claude) + Python-Funktion (Ausführung)
# ─────────────────────────────────────────────────────────────────────────────

TOOLS_SCHEMA = [
    {
        "name": "web_search",
        "description": (
            "Sucht im Web nach aktuellen Informationen. Nutze dies, wenn die "
            "Antwort von aktuellem Wissen abhängt (Ereignisse, Preise, Fakten "
            "nach deinem Trainingsstand)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Die Suchanfrage."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "file",
        "description": (
            "Liest oder schreibt eine Textdatei im Arbeitsordner. "
            "action='read' liefert den Inhalt; action='write' speichert 'content'. "
            "Pfade sind relativ zum Arbeitsordner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write"]},
                "path": {"type": "string", "description": "Relativer Dateipfad."},
                "content": {
                    "type": "string",
                    "description": "Nur bei action='write': der zu schreibende Text.",
                },
            },
            "required": ["action", "path"],
        },
    },
    {
        "name": "list_files",
        "description": "Listet Dateien und Ordner im Arbeitsordner (read-only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "subdir": {
                    "type": "string",
                    "description": "Optionaler Unterordner. Leer = Arbeitsordner.",
                }
            },
            "required": [],
        },
    },
]

# Name -> Python-Callable
TOOL_IMPL = {
    "web_search": lambda i: tool_web_search(i.get("query", "")),
    "file": lambda i: tool_file(i.get("action", ""), i.get("path", ""), i.get("content")),
    "list_files": lambda i: tool_list_files(i.get("subdir", "")),
}


def _execute_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Führt ein Tool aus. Rückgabe: (ergebnis_text, is_error).

    Fängt ALLE Fehler ab -> der Loop stürzt nie ab. Guardrail-Verstöße und
    leere/kaputte Ergebnisse werden als sauberer Fehlertext an Claude
    zurückgegeben, damit das Modell einen anderen Weg wählen kann.
    """
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return (f"Unbekanntes Tool '{name}'.", True)
    try:
        result = impl(tool_input or {})
        if result is None or (isinstance(result, str) and not result.strip()):
            return ("Tool lieferte ein leeres Ergebnis.", True)  # Fallback
        return (str(result), False)
    except GuardrailError as exc:
        return (f"GUARDRAIL: {exc}", True)
    except Exception as exc:
        return (f"Fehler in Tool '{name}': {type(exc).__name__}: {exc}", True)


# ─────────────────────────────────────────────────────────────────────────────
# Kontrollschleife
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Du bist ein autonomer Assistent mit Werkzeugen. Entscheide selbst, welche "
    "Tools du in welcher Reihenfolge brauchst, und arbeite eigenständig bis zum "
    "Ergebnis. Alle Dateioperationen laufen in einem festen Arbeitsordner. "
    "Wenn ein Tool einen Fehler liefert, versuche einen anderen Weg statt "
    "aufzugeben. Wenn du fertig bist, antworte dem Nutzer direkt in Prosa."
)


def run_agent(user_request: str, verbose: bool = True) -> str:
    """Führt eine Anfrage autonom aus und gibt die finale Textantwort zurück.

    Das ist die öffentliche Schnittstelle des Agent-Kerns. Der Voice-Layer
    (Phase 2) ruft ausschließlich diese Funktion auf.
    """
    client = anthropic.Anthropic()  # liest ANTHROPIC_API_KEY aus der Umgebung
    messages: list[dict] = [{"role": "user", "content": user_request}]

    for iteration in range(1, MAX_ITERATIONS + 1):
        if verbose:
            print(f"\n── Iteration {iteration}/{MAX_ITERATIONS} ──")

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS_SCHEMA,
                messages=messages,
            )
        except anthropic.APIError as exc:
            return f"[Abbruch] API-Fehler: {exc}"

        # Sicherheitsablehnung: erst stop_reason prüfen, dann content lesen.
        if response.stop_reason == "refusal":
            return "[Abbruch] Anfrage wurde aus Sicherheitsgründen abgelehnt."

        # Modell ist fertig -> finalen Text zurückgeben.
        if response.stop_reason != "tool_use":
            final = "".join(b.text for b in response.content if b.type == "text")
            return final.strip() or "[Ende] Keine Textantwort erzeugt."

        # Assistenten-Turn (inkl. tool_use-Blöcke) an die Historie anhängen.
        messages.append({"role": "assistant", "content": response.content})

        # Alle angeforderten Tools ausführen und Ergebnisse sammeln.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if verbose:
                print(f"  → Tool: {block.name}  Input: {json.dumps(block.input, ensure_ascii=False)}")
            result_text, is_error = _execute_tool(block.name, block.input)
            if verbose:
                preview = result_text if len(result_text) <= 200 else result_text[:200] + "…"
                print(f"    {'✗' if is_error else '✓'} {preview}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
                "is_error": is_error,
            })

        # Alle Tool-Ergebnisse in EINER User-Nachricht zurückgeben.
        messages.append({"role": "user", "content": tool_results})

    # Iterationslimit erreicht -> sauberer Abbruch, kein Crash.
    return (
        f"[Abbruch] Iterationslimit ({MAX_ITERATIONS}) erreicht, ohne die "
        "Aufgabe abzuschließen. Bitte die Anfrage konkretisieren oder aufteilen."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Terminal-Einstieg
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Autonomer Tool-Agent — Text-Modus. Arbeitsordner:", WORK_DIR)
    print("Leere Eingabe oder 'exit' beendet.\n")
    while True:
        try:
            request = input("Du > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not request or request.lower() in {"exit", "quit"}:
            break
        answer = run_agent(request)
        print("\nAgent >", answer, "\n")


if __name__ == "__main__":
    main()
