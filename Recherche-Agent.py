#!/usr/bin/env python3
"""
============================================================================
  AUTONOMER RECHERCHE-AGENT  —  alles in EINER Datei
============================================================================

So startest du:
  • Windows: Diese Datei doppelklicken (Python muss installiert sein:
             https://www.python.org/downloads/ — beim Setup
             "Add Python to PATH" anhaken).
  • Mac/Linux: Terminal öffnen, dann:  python3 "Recherche-Agent.py"

Diese Datei installiert das benötigte Paket selbst und fragt beim ersten
Start einmal nach deinem API-Key (danach geht's direkt).
Einen Key gibt es kostenlos unter https://console.anthropic.com/
(Settings → API Keys, beginnt mit "sk-ant-").

Der Agent sucht im Web, liest ganze Seiten, gleicht Quellen ab und kann
Ergebnisse in einem Arbeitsordner speichern. Modell: Claude Fable 5.
============================================================================
"""

from __future__ import annotations

# ─── 0) Benötigtes Paket automatisch installieren ───────────────────────────
import importlib
import subprocess
import sys


def _ensure(pkg: str) -> None:
    try:
        importlib.import_module(pkg)
    except ImportError:
        print(f"Installiere einmalig das Paket '{pkg}' ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", pkg]
        )


_ensure("anthropic")

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

# ─── 1) Konfiguration ───────────────────────────────────────────────────────
MODEL = "claude-fable-5"            # leistungsfähigstes Modell, 1M Kontext
FALLBACK_MODEL = "claude-opus-4-8"  # springt bei Sicherheits-Ablehnung ein
EFFORT = "high"                     # gründliche Recherche
MAX_TOKENS = 8192
MAX_ITERATIONS = 15                 # HARTES LIMIT
MAX_FILE_BYTES = 2_000_000
MAX_FETCH_BYTES = 3_000_000
MAX_FETCH_CHARS = 40_000
MAX_SEARCH_RESULTS = 10
MAX_CONTEXT_CHARS = 200_000         # Deckel gegen unbegrenztes Gedächtnis-Wachstum
MAX_RETRIES = 4                     # SDK-Retries für transiente Fehler (429/5xx)

_HERE = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("AGENT_WORKDIR", _HERE / "agent_workspace")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
_KEY_FILE = _HERE / ".api_key"
CONFIG_FILE = _HERE / ".agent_config.json"


def load_config() -> dict:
    """Lädt .agent_config.json (Such-Keys -> Umgebung, effort -> Modul)."""
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


# ─── 2) Guardrails ──────────────────────────────────────────────────────────
# Im Code verankert (nicht nur im Prompt):
#   • Dateien nur innerhalb WORK_DIR (kein "../", keine absoluten Ausbrüche)
#   • fetch_url nur öffentliche http/https (kein SSRF auf localhost/intern)
#   • Größenlimits, hartes Iterationslimit, kein Shell/kein Code-Tool
class GuardrailError(Exception):
    pass


def _safe_path(raw: str) -> Path:
    if not raw or not isinstance(raw, str):
        raise GuardrailError("Leerer oder ungültiger Pfad.")
    cand = (WORK_DIR / raw).resolve()
    if cand != WORK_DIR and WORK_DIR not in cand.parents:
        raise GuardrailError(f"Zugriff verweigert: '{raw}' liegt außerhalb des Arbeitsordners.")
    return cand


def _assert_public_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise GuardrailError(f"Nur http/https erlaubt, nicht '{p.scheme}'.")
    if not p.hostname:
        raise GuardrailError("URL ohne Host.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise GuardrailError(f"Host nicht auflösbar: {p.hostname} ({exc}).")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise GuardrailError(f"Zugriff auf interne/reservierte Adresse verweigert: {ip}.")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prüft bei JEDEM 30x-Redirect das Sprungziel erneut (sonst SSRF-Bypass)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


# ─── 3) Tools ───────────────────────────────────────────────────────────────
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
    # Reihenfolge: Tavily/Brave (falls Key als Umgebungsvariable) -> DuckDuckGo
    # HTML (echte Treffer, schlüssellos) -> Instant Answer -> Stub.
    query = (query or "").strip()
    if not query:
        return "[Websuche] Leere Suchanfrage."

    def _fmt(res: list[str]) -> str:
        lines = [f"{i}. {r}" for i, r in enumerate(res[:MAX_SEARCH_RESULTS], 1)]
        return "[Websuche] Treffer (URLs mit fetch_url öffnen):\n" + "\n".join(lines)

    for env, fn in (("TAVILY_API_KEY", _search_tavily), ("BRAVE_API_KEY", _search_brave)):
        key = os.environ.get(env)
        if key:
            try:
                res = fn(query, key, MAX_SEARCH_RESULTS)
                if res:
                    return _fmt(res)
            except Exception:
                pass
    try:
        res = _search_ddg_html(query, MAX_SEARCH_RESULTS)
        if res:
            return _fmt(res)
    except Exception:
        pass
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


_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")


def tool_fetch_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "[Fetch] Leere URL."
    _assert_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "tool-agent/1.0"})
    with _SAFE_OPENER.open(req, timeout=12) as resp:   # Redirects werden erneut geprüft
        _assert_public_url(resp.geturl())
        raw = resp.read(MAX_FETCH_BYTES + 1)
        ctype = resp.headers.get("Content-Type", "")
    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    body = raw.decode("utf-8", errors="replace")
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


def tool_file(action: str, path: str, content: str | None = None) -> str:
    action = (action or "").lower().strip()
    target = _safe_path(path)
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


def tool_list_files(subdir: str = "") -> str:
    base = _safe_path(subdir) if subdir else WORK_DIR
    if not base.exists():
        return f"[Liste] Ordner existiert nicht: {subdir or '.'}"
    entries = sorted(base.iterdir())
    if not entries:
        return "[Liste] Arbeitsordner ist leer."
    out = []
    for e in entries:
        kind = "DIR " if e.is_dir() else "FILE"
        size = e.stat().st_size if e.is_file() else 0
        out.append(f"{kind} {e.name} ({size} B)")
    return "[Liste]\n" + "\n".join(out)


# ─── 4) Tool-Registry (JSON-Schema für Claude + Python-Funktion) ────────────
TOOLS_SCHEMA = [
    {"name": "web_search",
     "description": "Sucht im Web und liefert Treffer MIT URL. Danach die "
                    "interessanten Seiten mit fetch_url im Detail lesen.",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string", "description": "Die Suchanfrage."}},
                      "required": ["query"]}},
    {"name": "fetch_url",
     "description": "Lädt eine öffentliche Webseite und gibt ihren Text zurück "
                    "(volle Artikel statt nur Snippets). Nur http/https.",
     "input_schema": {"type": "object",
                      "properties": {"url": {"type": "string", "description": "Vollständige http(s)-URL."}},
                      "required": ["url"]}},
    {"name": "file",
     "description": "Liest/schreibt eine Textdatei im Arbeitsordner. action='read' "
                    "liefert Inhalt; action='write' speichert 'content'.",
     "input_schema": {"type": "object",
                      "properties": {"action": {"type": "string", "enum": ["read", "write"]},
                                     "path": {"type": "string", "description": "Relativer Dateipfad."},
                                     "content": {"type": "string", "description": "Nur bei write: der Text."}},
                      "required": ["action", "path"]}},
    {"name": "list_files",
     "description": "Listet Dateien im Arbeitsordner (read-only).",
     "input_schema": {"type": "object",
                      "properties": {"subdir": {"type": "string", "description": "Optionaler Unterordner."}},
                      "required": []}},
]

TOOL_IMPL = {
    "web_search": lambda i: tool_web_search(i.get("query", "")),
    "fetch_url": lambda i: tool_fetch_url(i.get("url", "")),
    "file": lambda i: tool_file(i.get("action", ""), i.get("path", ""), i.get("content")),
    "list_files": lambda i: tool_list_files(i.get("subdir", "")),
}


def _execute_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return (f"Unbekanntes Tool '{name}'.", True)
    try:
        result = impl(tool_input or {})
        if result is None or (isinstance(result, str) and not result.strip()):
            return ("Tool lieferte ein leeres Ergebnis.", True)
        return (str(result), False)
    except GuardrailError as exc:
        return (f"GUARDRAIL: {exc}", True)
    except Exception as exc:
        return (f"Fehler in Tool '{name}': {type(exc).__name__}: {exc}", True)


# ─── 5) Kontrollschleife ────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Du bist ein gründlicher, autonomer Recherche-Assistent mit Werkzeugen. "
    "Beschaffe dir so viel relevante Information wie nötig: nutze web_search, um "
    "Quellen/URLs zu finden, und fetch_url, um die besten Seiten vollständig zu "
    "lesen. Ziehe mehrere Quellen heran und gleiche sie ab; nenne Quellen. "
    "Speichere umfangreiche Zwischenstände bei Bedarf mit file. Entscheide selbst "
    "über Reihenfolge und Anzahl der Tool-Aufrufe. Bei Tool-Fehlern anderen Weg "
    "versuchen. Bist du fertig, antworte direkt und klar, Ergebnis zuerst."
)


def _system_prompt() -> str:
    return SYSTEM_PROMPT + f"\n\nHeutiges Datum: {datetime.date.today().isoformat()}."


def _create_message(client: "anthropic.Anthropic", messages: list[dict]):
    common = dict(model=MODEL, max_tokens=MAX_TOKENS, system=_system_prompt(),
                  tools=TOOLS_SCHEMA, messages=messages,
                  output_config={"effort": EFFORT})
    try:
        return client.beta.messages.create(
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": FALLBACK_MODEL}], **common)
    except (TypeError, AttributeError, anthropic.BadRequestError):
        try:
            return client.messages.create(**common)
        except (TypeError, anthropic.BadRequestError):
            common.pop("output_config", None)
            return client.messages.create(**common)


def _stream_message(client, messages, on_delta):
    """Wie _create_message, aber gestreamt (on_delta pro Text-Delta). Fällt bei
    fehlender Streaming-Fähigkeit transparent auf _create_message zurück."""
    common = dict(model=MODEL, max_tokens=MAX_TOKENS, system=_system_prompt(),
                  tools=TOOLS_SCHEMA, messages=messages,
                  output_config={"effort": EFFORT})
    for opener in (
        lambda: client.beta.messages.stream(
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": FALLBACK_MODEL}], **common),
        lambda: client.messages.stream(**common),
    ):
        try:
            with opener() as stream:
                for text in stream.text_stream:
                    if text:
                        on_delta(text)
                return stream.get_final_message()
        except (TypeError, AttributeError, anthropic.BadRequestError):
            continue
    return _create_message(client, messages)


def _emitter(verbose, on_step):
    def emit(msg):
        if on_step is not None:
            on_step(msg)
        elif verbose:
            print(msg)
    return emit


def _finalize(messages: list[dict], text: str) -> tuple[str, list[dict]]:
    """JEDER Rückgabepfad endet über diese Funktion mit einem assistant-Turn,
    damit die Rollen immer sauber alternieren — auch bei Abbruch. Sonst würde
    die Historie auf einem user-Turn enden und die nächste Frage einen zweiten
    user-Turn anhängen (vergiftetes Gespräch)."""
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
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return True


_TRUNC_MARK = " [gekuerzt]"


def _truncate_oldest_tool_results(messages, max_chars, min_keep=800):
    """Kürzt Inhalte der ältesten tool_result-Blöcke (alt → neu) unter max_chars,
    ohne Turn-Struktur/Paarung anzutasten. Greift auch in einer Riesenrunde."""
    for m in messages:
        if sum(_msg_size(x) for x in messages) <= max_chars:
            break
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if sum(_msg_size(x) for x in messages) <= max_chars:
                break
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and isinstance(b.get("content"), str) and len(b["content"]) > min_keep):
                over = sum(_msg_size(x) for x in messages) - max_chars
                c = b["content"]
                cut = min(over + len(_TRUNC_MARK), len(c) - min_keep)
                b["content"] = c[:len(c) - cut] + _TRUNC_MARK
    return messages


def _trim(messages: list[dict], max_chars: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """Stufe 1: ganze frühere Runden verwerfen (Paare bleiben ganz, aktive Runde
    bleibt). Stufe 2: reicht das nicht (einzelne Riesenrunde / zustandsloses
    run_agent), Inhalte der ältesten tool_result-Blöcke kürzen. Ergebnis beginnt
    immer mit einer User-Frage."""
    if sum(_msg_size(m) for m in messages) <= max_chars:
        return messages
    bounds = [i for i, m in enumerate(messages) if _is_user_question(m)]
    if len(bounds) >= 2:
        last_round = bounds[-1]
        chosen = last_round
        for b in bounds:
            if b >= last_round:
                break
            if sum(_msg_size(m) for m in messages[b:]) <= max_chars:
                chosen = b
                break
        messages = messages[chosen:]
    if sum(_msg_size(m) for m in messages) > max_chars:
        messages = _truncate_oldest_tool_results(messages, max_chars)
    return messages


def _run_loop(client, messages: list[dict], emit, on_delta=None,
              should_cancel=None) -> tuple[str, list[dict]]:
    for iteration in range(1, MAX_ITERATIONS + 1):
        if should_cancel is not None and should_cancel():
            return _finalize(messages, "[Abgebrochen] Auf Wunsch gestoppt.")
        messages = _trim(messages)  # Gedächtnis paarungssicher begrenzen
        emit(f"── Iteration {iteration}/{MAX_ITERATIONS} ──")
        try:
            if on_delta is not None:
                response = _stream_message(client, messages, on_delta)
            else:
                response = _create_message(client, messages)
        except anthropic.APIError as exc:
            return _finalize(messages, f"[Abbruch] API-Fehler: {exc}")
        if getattr(response, "model", MODEL) != MODEL:
            emit(f"(Fallback-Modell hat geantwortet: {response.model})")
        if response.stop_reason == "refusal":
            return _finalize(messages, "[Abbruch] Anfrage wurde aus Sicherheitsgründen abgelehnt.")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            final = "".join(b.text for b in response.content if b.type == "text").strip()
            return _finalize(messages, final or "[Ende] Keine Textantwort erzeugt.")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_uses:
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


def run_agent(user_request: str, verbose: bool = True, on_step=None,
              on_delta=None, should_cancel=None) -> str:
    """Einzelanfrage ohne Gedächtnis."""
    client = anthropic.Anthropic(max_retries=MAX_RETRIES)
    answer, _ = _run_loop(client, [{"role": "user", "content": user_request}],
                          _emitter(verbose, on_step),
                          on_delta=on_delta, should_cancel=should_cancel)
    return answer


class Agent:
    """Fortlaufendes Gespräch MIT Gedächtnis."""

    def __init__(self):
        self.client = anthropic.Anthropic(max_retries=MAX_RETRIES)
        self.messages: list[dict] = []

    def ask(self, user_request: str, verbose: bool = True, on_step=None,
            on_delta=None, should_cancel=None) -> str:
        self.messages.append({"role": "user", "content": user_request})
        answer, self.messages = _run_loop(
            self.client, self.messages, _emitter(verbose, on_step),
            on_delta=on_delta, should_cancel=should_cancel)
        return answer

    def reset(self) -> None:
        self.messages = []


# ─── 6) API-Key-Bootstrap ───────────────────────────────────────────────────
def ensure_api_key() -> bool:
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
            os.chmod(_KEY_FILE, 0o600)
        except OSError:
            pass
        print(f"\nKey gespeichert ({_KEY_FILE.name}) — beim nächsten Mal geht's direkt los.\n")
    except OSError as exc:
        print(f"\nKonnte Key nicht speichern ({exc}); gilt nur für diese Sitzung.\n")
    return True


# ─── 7) Start ───────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print(f"  Autonomer Recherche-Agent  ({MODEL})")
    print("=" * 60)
    load_config()
    if not ensure_api_key():
        print("Ohne API-Key kann der Agent nicht starten.")
        input("Enter zum Schließen ...")
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
        try:
            answer = agent.ask(request)
        except Exception as exc:  # nichts soll das Fenster hart abstürzen lassen
            answer = f"[Fehler] {type(exc).__name__}: {exc}"
        print("\nAgent >", answer, "\n")
    input("Enter zum Schließen ...")


if __name__ == "__main__":
    main()
