"""
Phase 1 — Autonomer Recherche-Agent (Text rein / Text raus, Terminal).

Ein Agent, der eine Anfrage bekommt, selbst entscheidet welche Tools er in
welcher Reihenfolge aufruft, und autonom bis zum Ergebnis arbeitet. Ausgelegt
darauf, sich VIEL Information zu beschaffen: Websuche liefert Quell-URLs,
fetch_url lädt ganze Seiten, mehrere Quellen werden abgeglichen; Fable 5 hält
das alles in seinem 1M-Kontext.

Reasoning-Loop: Anthropic Messages API (manuelle Kontrollschleife, damit das
harte Iterationslimit und die Guardrails vollständig im Code liegen).

Modell: claude-fable-5 (Anthropics leistungsfähigstes Modell). Bei einer
sicherheitsbedingten Ablehnung übernimmt automatisch ein Fallback-Modell
(server-side fallback). Fable 5 setzt 30-Tage-Datenaufbewahrung voraus — bei
Zero-Data-Retention-Orgs schlägt JEDE Anfrage mit 400 fehl.

KEIN API-Key im Code. Setze ihn als Umgebungsvariable:
    export ANTHROPIC_API_KEY="sk-ant-..."      # Linux / macOS
    setx  ANTHROPIC_API_KEY "sk-ant-..."       # Windows (neues Terminal öffnen)

Start:
    pip install anthropic
    python agent.py
"""

from __future__ import annotations

import datetime
import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

MODEL = "claude-fable-5"          # leistungsfähigstes Modell, 1M Kontext
FALLBACK_MODEL = "claude-opus-4-8"  # springt bei Sicherheits-Ablehnung ein
EFFORT = "high"                    # gründliche Recherche (low|medium|high|xhigh|max)

MAX_TOKENS = 8192                  # Antwort-/Turn-Budget (nicht-gestreamt, < ~16k)
MAX_ITERATIONS = 15               # HARTES LIMIT — Raum für Multi-Quellen-Recherche

MAX_FILE_BYTES = 2_000_000        # 2 MB Lese-/Schreib-Obergrenze pro Datei
MAX_FETCH_BYTES = 3_000_000       # max. Download-Größe einer Seite (roh)
MAX_FETCH_CHARS = 40_000          # max. an das Modell zurückgegebener Text/Seite
MAX_SEARCH_RESULTS = 10           # Treffer pro Websuche
MAX_CONTEXT_CHARS = 200_000       # grober Deckel gegen unbegrenztes Gedächtnis-Wachstum
MAX_RETRIES = 4                   # SDK-Retries für transiente Fehler (429/5xx)

# Arbeitsordner: alles läuft ausschließlich hier drin. Wird beim Start angelegt.
_HERE = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("AGENT_WORKDIR", "./agent_workspace")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Lokale Konfiguration (Suchschlüssel, Effort) — wird nicht committet.
CONFIG_FILE = _HERE / ".agent_config.json"


def load_config() -> dict:
    """Lädt .agent_config.json und wendet sie an (Such-Keys -> Umgebung,
    effort -> Modul). Wird beim Start aufgerufen. Fehlt die Datei, passiert
    nichts."""
    global EFFORT
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for name in ("TAVILY_API_KEY", "BRAVE_API_KEY"):
        val = (cfg.get(name) or "").strip()
        if val:
            os.environ[name] = val
    if cfg.get("effort") in ("low", "medium", "high", "xhigh", "max"):
        EFFORT = cfg["effort"]
    return cfg


def save_config(values: dict) -> None:
    """Speichert Such-Keys/Effort in .agent_config.json und wendet sie sofort an."""
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cfg.update(values)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    load_config()


# ─────────────────────────────────────────────────────────────────────────────
# Guardrails
# ─────────────────────────────────────────────────────────────────────────────
#
# Was der Agent NIE tut (im Code verankert, nicht nur im System-Prompt):
#   1. Dateien außerhalb von WORK_DIR lesen/schreiben  -> _safe_path()
#   2. Dateien / Downloads über die Größenlimits        -> Byte-Checks
#   3. Interne/lokale Adressen abrufen (SSRF)           -> _assert_public_url()
#      fetch_url darf NUR öffentliche http/https-Hosts laden — keine
#      localhost/127.*, keine privaten Netze, kein Cloud-Metadaten-Endpunkt.
#   4. Systembefehle / Shell / beliebiger Python-Code   -> es gibt kein Tool dafür
#   5. Mehr als MAX_ITERATIONS Runden                   -> Kontrollschleife
#
# Sicherheitsgrenze: Tool-Eingaben sind Modell-Output und damit nicht
# vertrauenswürdig. Jede Datei- und Netz-Operation wird deshalb hart validiert.


class GuardrailError(Exception):
    """Wird ausgelöst, wenn eine Aktion eine Sicherheitsgrenze verletzt."""


def _safe_path(raw_path: str) -> Path:
    """Löst einen vom Modell gelieferten Pfad relativ zu WORK_DIR auf und
    stellt sicher, dass er WORK_DIR nicht verlässt. Sonst GuardrailError."""
    if not raw_path or not isinstance(raw_path, str):
        raise GuardrailError("Leerer oder ungültiger Pfad.")
    candidate = (WORK_DIR / raw_path).resolve()
    if candidate != WORK_DIR and WORK_DIR not in candidate.parents:
        raise GuardrailError(
            f"Zugriff verweigert: '{raw_path}' liegt außerhalb des Arbeitsordners."
        )
    return candidate


def _assert_public_url(url: str) -> str:
    """Erlaubt nur öffentliche http/https-URLs. Blockiert SSRF: localhost,
    Loopback, private/reservierte/Link-Local-Netze (inkl. Cloud-Metadaten
    169.254.169.254). Prüft ALLE aufgelösten IP-Adressen des Hosts."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GuardrailError(f"Nur http/https erlaubt, nicht '{parsed.scheme}'.")
    host = parsed.hostname
    if not host:
        raise GuardrailError("URL ohne Host.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise GuardrailError(f"Host nicht auflösbar: {host} ({exc}).")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise GuardrailError(f"Zugriff auf interne/reservierte Adresse verweigert: {ip}.")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Websuche (liefert Text + URLs zum Nachladen)
# ─────────────────────────────────────────────────────────────────────────────
#
# Schlüssellose DuckDuckGo Instant-Answer-API. Gibt Treffer MIT URL zurück,
# damit der Agent anschließend fetch_url auf die spannendsten Quellen anwenden
# kann. Kein Netz/kein Treffer -> klar markierter Stub, kein Crash. Für echte
# Volltextsuche hier einen Dienst mit Key eintragen (Brave, Tavily, SerpAPI).

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _strip_html(s: str) -> str:
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _decode_ddg_link(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return params.get("uddg", [href])[0]


def _search_ddg_html(query: str, limit: int) -> list[str]:
    """Schlüssellose echte Websuche über die DuckDuckGo-HTML-Seite."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    out: list[str] = []
    for i, (href, title) in enumerate(links[:limit]):
        snip = _strip_html(snips[i]) if i < len(snips) else ""
        out.append(f"{_strip_html(title)} — {snip}  <{_decode_ddg_link(href)}>")
    return out


def _search_tavily(query: str, key: str, limit: int) -> list[str]:
    payload = json.dumps({"api_key": key, "query": query, "max_results": limit}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return [f"{r.get('title', '')} — {r.get('content', '')}  <{r.get('url', '')}>"
            for r in data.get("results", [])[:limit]]


def _search_brave(query: str, key: str, limit: int) -> list[str]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": limit})
    req = urllib.request.Request(url, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return [f"{r.get('title', '')} — {r.get('description', '')}  <{r.get('url', '')}>"
            for r in data.get("web", {}).get("results", [])[:limit]]


def tool_web_search(query: str) -> str:
    """Echte Websuche. Reihenfolge:
    1. Tavily / Brave, falls TAVILY_API_KEY bzw. BRAVE_API_KEY gesetzt ist.
    2. Schlüssellose DuckDuckGo-HTML-Suche (echte Treffer, kein Key nötig).
    3. DuckDuckGo Instant Answer als letzter Versuch, sonst Stub.
    """
    query = (query or "").strip()
    if not query:
        return "[Websuche] Leere Suchanfrage."

    def _fmt(res: list[str]) -> str:
        lines = [f"{i}. {r}" for i, r in enumerate(res[:MAX_SEARCH_RESULTS], 1)]
        return "[Websuche] Treffer (URLs mit fetch_url öffnen):\n" + "\n".join(lines)

    # 1) Echte Such-API per Umgebungsvariable
    for env, fn in (("TAVILY_API_KEY", _search_tavily), ("BRAVE_API_KEY", _search_brave)):
        key = os.environ.get(env)
        if key:
            try:
                res = fn(query, key, MAX_SEARCH_RESULTS)
                if res:
                    return _fmt(res)
            except Exception:
                pass  # nächste Quelle probieren

    # 2) Schlüssellose echte Suche
    try:
        res = _search_ddg_html(query, MAX_SEARCH_RESULTS)
        if res:
            return _fmt(res)
    except Exception:
        pass

    # 3) Instant Answer / Stub
    try:
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "no_html": "1", "no_redirect": "1"})
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("AbstractText"):
            return f"[Websuche] {data['AbstractText']}  <{data.get('AbstractURL', '')}>"
    except Exception:
        pass
    return (f"[Websuche – STUB] Keine Treffer für '{query}'. Für stärkere Suche "
            "optional TAVILY_API_KEY oder BRAVE_API_KEY setzen.")


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — Ganze Webseite laden und als Text zurückgeben
# ─────────────────────────────────────────────────────────────────────────────
#
# Das ist der Schlüssel zu "viele Informationen": statt nur Suchsnippets kann
# der Agent vollständige Artikel/Dokus lesen. HTML wird grob zu Text gestrippt.

_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")


def tool_fetch_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "[Fetch] Leere URL."
    _assert_public_url(url)  # Guardrail: kein SSRF

    req = urllib.request.Request(url, headers={"User-Agent": "tool-agent/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read(MAX_FETCH_BYTES + 1)
        ctype = resp.headers.get("Content-Type", "")

    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    body = raw.decode("utf-8", errors="replace")

    # Bei HTML: Skripte/Styles entfernen, Tags strippen, Entities dekodieren.
    if "html" in ctype.lower() or body.lstrip()[:1] == "<":
        body = _SCRIPT_RE.sub(" ", body)
        body = _TAG_RE.sub(" ", body)
        body = html.unescape(body)
    text = re.sub(r"\s+", " ", body).strip()

    if not text:
        return f"[Fetch] {url} lieferte keinen lesbaren Text."
    if len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS] + f"\n…[gekürzt, {MAX_FETCH_CHARS} Zeichen]"
    return f"[Fetch] {url}\n{text}"


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — Datei lesen / schreiben (im Arbeitsordner)
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
# Tool 4 — Dateien im Arbeitsordner auflisten
# ─────────────────────────────────────────────────────────────────────────────

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
            "Sucht im Web und liefert Treffer MIT URL. Nutze dies, um Quellen "
            "und Links zu finden; lies die interessanten Seiten anschließend mit "
            "fetch_url im Detail."
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
        "name": "fetch_url",
        "description": (
            "Lädt eine öffentliche Webseite und gibt ihren Textinhalt zurück. "
            "Damit liest du vollständige Artikel/Quellen statt nur Snippets. "
            "Nur http/https, keine internen Adressen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Vollständige http(s)-URL."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "file",
        "description": (
            "Liest oder schreibt eine Textdatei im Arbeitsordner. "
            "action='read' liefert den Inhalt; action='write' speichert 'content'. "
            "Nutze dies, um recherchierte Informationen zu sammeln/zwischenzuspeichern."
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
    "fetch_url": lambda i: tool_fetch_url(i.get("url", "")),
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
    "Du bist ein gründlicher, autonomer Recherche-Assistent mit Werkzeugen. "
    "Beschaffe dir so viel relevante Information wie nötig: nutze web_search, "
    "um Quellen und URLs zu finden, und fetch_url, um die vielversprechendsten "
    "Seiten vollständig zu lesen. Ziehe mehrere Quellen heran und gleiche sie ab, "
    "bevor du antwortest; nenne Quellen. Speichere umfangreiche Zwischenstände "
    "bei Bedarf mit dem file-Tool im Arbeitsordner. Entscheide selbst über "
    "Reihenfolge und Anzahl der Tool-Aufrufe. Wenn ein Tool einen Fehler liefert, "
    "versuche einen anderen Weg statt aufzugeben. In einem Gespräch beziehst du "
    "dich auf vorherige Fragen und Antworten. Bist du fertig, antworte dem Nutzer "
    "direkt und klar in Prosa, Ergebnis zuerst."
)


def _system_prompt() -> str:
    """System-Prompt inkl. heutigem Datum (für 'aktuell/neueste')."""
    return SYSTEM_PROMPT + f"\n\nHeutiges Datum: {datetime.date.today().isoformat()}."


def _create_message(client: "anthropic.Anthropic", messages: list[dict]):
    """Erzeugt eine Antwort. Für Fable 5 mit Server-Side-Fallback auf ein
    Ausweichmodell (falls eine Sicherheits-Ablehnung greift). Degradiert
    robust, falls die SDK-Version die Parameter nicht kennt."""
    common = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_system_prompt(),
        tools=TOOLS_SCHEMA,
        messages=messages,
        output_config={"effort": EFFORT},
    )
    try:
        return client.beta.messages.create(
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": FALLBACK_MODEL}],
            **common,
        )
    except (TypeError, AttributeError, anthropic.BadRequestError):
        try:
            return client.messages.create(**common)          # ohne Fallback
        except (TypeError, anthropic.BadRequestError):
            common.pop("output_config", None)                 # ohne effort
            return client.messages.create(**common)


def _finalize(messages: list[dict], text: str) -> tuple[str, list[dict]]:
    """JEDER Rückgabepfad endet über diese Funktion mit einem assistant-Turn.

    Damit alternieren die Rollen immer sauber (…user, assistant) — auch bei
    Abbruch (API-Fehler, refusal, Iterationslimit). Sonst würde die Historie auf
    einem user-Turn enden, die nächste Frage einen zweiten user-Turn anhängen und
    das Gespräch dauerhaft vergiften. (Fix für den Memory-Bug.)
    """
    messages.append({"role": "assistant", "content": text})
    return text, messages


def _msg_size(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    try:
        return len(json.dumps(c, default=str))
    except Exception:
        return len(str(c))


def _is_user_question(m: dict) -> bool:
    """True für einen 'echten' User-Turn (Frage), False für tool_result-Turns.
    Nur an solchen Grenzen darf die Historie gekürzt werden."""
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return True


def _trim(messages: list[dict], max_chars: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """Begrenzt die Historie, ohne tool_use/tool_result-Paare zu zerreißen.

    Wirft ganze Gesprächsrunden vom Anfang weg (jede Runde beginnt mit einer
    User-Frage). Die aktuelle (letzte) Runde bleibt immer vollständig erhalten,
    und das Ergebnis beginnt immer mit einer User-Frage.
    """
    if sum(_msg_size(m) for m in messages) <= max_chars:
        return messages
    bounds = [i for i, m in enumerate(messages) if _is_user_question(m)]
    if len(bounds) < 2:
        return messages  # nur die aktuelle Runde -> nichts sinnvoll wegzuwerfen
    last_round = bounds[-1]
    for b in bounds:
        if b >= last_round:
            break
        if sum(_msg_size(m) for m in messages[b:]) <= max_chars:
            return messages[b:]
    return messages[last_round:]  # notfalls nur die letzte Runde behalten


def _run_loop(client, messages: list[dict], emit) -> tuple[str, list[dict]]:
    """Autonome Kontrollschleife über eine bestehende Nachrichten-Historie.

    Rückgabe: (finale_antwort, aktualisierte_historie). Über _finalize endet die
    Historie IMMER auf einem assistant-Turn, sodass Folgefragen sauber anknüpfen.
    """
    for iteration in range(1, MAX_ITERATIONS + 1):
        messages = _trim(messages)  # Gedächtnis paarungssicher begrenzen
        emit(f"── Iteration {iteration}/{MAX_ITERATIONS} ──")
        try:
            response = _create_message(client, messages)
        except anthropic.APIError as exc:
            return _finalize(messages, f"[Abbruch] API-Fehler: {exc}")

        if getattr(response, "model", MODEL) != MODEL:
            emit(f"(Fallback-Modell hat geantwortet: {response.model})")

        if response.stop_reason == "refusal":
            return _finalize(messages, "[Abbruch] Anfrage wurde aus Sicherheitsgründen abgelehnt.")

        if response.stop_reason != "tool_use":
            final = "".join(b.text for b in response.content if b.type == "text").strip()
            return _finalize(messages, final or "[Ende] Keine Textantwort erzeugt.")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            emit(f"→ Tool: {block.name}  {json.dumps(block.input, ensure_ascii=False)[:160]}")
            result_text, is_error = _execute_tool(block.name, block.input)
            preview = result_text if len(result_text) <= 200 else result_text[:200] + "…"
            emit(f"  {'✗' if is_error else '✓'} {preview}")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": result_text, "is_error": is_error})
        messages.append({"role": "user", "content": tool_results})

    return _finalize(messages, f"[Abbruch] Iterationslimit ({MAX_ITERATIONS}) erreicht, "
                     "ohne die Aufgabe abzuschließen. Bitte die Anfrage konkretisieren "
                     "oder aufteilen.")


def _emitter(verbose: bool, on_step):
    def emit(msg: str) -> None:
        if on_step is not None:
            on_step(msg)
        elif verbose:
            print(msg)
    return emit


def run_agent(user_request: str, verbose: bool = True, on_step=None) -> str:
    """Einzelanfrage ohne Gedächtnis (Rückgabe: finale Textantwort).

    Für ein fortlaufendes Gespräch mit Gedächtnis die Klasse Agent nutzen.
    """
    client = anthropic.Anthropic(max_retries=MAX_RETRIES)
    messages = [{"role": "user", "content": user_request}]
    answer, _ = _run_loop(client, messages, _emitter(verbose, on_step))
    return answer


class Agent:
    """Fortlaufendes Gespräch MIT Gedächtnis. Jede Frage kennt die vorherigen.

        a = Agent(); a.ask("Wer ist X?"); a.ask("Und wo wurde er geboren?")
        a.reset()  # neues Gespräch
    """

    def __init__(self):
        # max_retries: transiente 429/5xx werden mehrfach automatisch wiederholt.
        self.client = anthropic.Anthropic(max_retries=MAX_RETRIES)
        self.messages: list[dict] = []

    def ask(self, user_request: str, verbose: bool = True, on_step=None) -> str:
        self.messages.append({"role": "user", "content": user_request})
        answer, self.messages = _run_loop(self.client, self.messages,
                                          _emitter(verbose, on_step))
        return answer

    def reset(self) -> None:
        self.messages = []


# ─────────────────────────────────────────────────────────────────────────────
# API-Key-Bootstrap (für Doppelklick-Start ohne Terminal-Befehle)
# ─────────────────────────────────────────────────────────────────────────────

_KEY_FILE = Path(__file__).resolve().parent / ".api_key"


def ensure_api_key() -> bool:
    """Stellt sicher, dass ANTHROPIC_API_KEY gesetzt ist.

    Reihenfolge: Umgebungsvariable -> lokale Datei .api_key -> interaktive
    Abfrage (wird dann in .api_key gespeichert, damit man es nur einmal machen
    muss). Der Key steht NICHT im Code und wird nicht committet (.gitignore).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if _KEY_FILE.exists():
        key = _KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
    print("Kein API-Key gefunden.")
    print("Einen Key bekommst du kostenlos unter https://console.anthropic.com/")
    print("(Settings → API Keys). Er beginnt mit 'sk-ant-'.\n")
    try:
        key = input("API-Key hier einfügen und Enter drücken: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not key:
        return False
    os.environ["ANTHROPIC_API_KEY"] = key
    try:
        _KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(_KEY_FILE, 0o600)  # nur für dich lesbar (POSIX)
        except OSError:
            pass
        print(f"\nKey gespeichert in {_KEY_FILE.name} — beim nächsten Mal geht's direkt los.\n")
    except OSError as exc:
        print(f"\nKonnte Key nicht speichern ({exc}); gilt nur für diese Sitzung.\n")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Terminal-Einstieg
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Autonomer Recherche-Agent ({MODEL}) — Text-Modus.")
    load_config()
    if not ensure_api_key():
        print("Ohne API-Key kann der Agent nicht starten. Beende.")
        return
    print("Arbeitsordner:", WORK_DIR)
    print("Gesprächsgedächtnis aktiv — Folgefragen bauen aufeinander auf.")
    print("'neu' startet ein neues Gespräch, leere Eingabe oder 'exit' beendet.\n")
    agent = Agent()
    while True:
        try:
            request = input("Du > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not request or request.lower() in {"exit", "quit"}:
            break
        if request.lower() in {"neu", "reset"}:
            agent.reset()
            print("(neues Gespräch gestartet)\n")
            continue
        answer = agent.ask(request)
        print("\nAgent >", answer, "\n")


if __name__ == "__main__":
    main()
