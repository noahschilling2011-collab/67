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

_HERE = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("AGENT_WORKDIR", _HERE / "agent_workspace")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
_KEY_FILE = _HERE / ".api_key"


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
    with urllib.request.urlopen(req, timeout=12) as resp:
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


def _create_message(client: "anthropic.Anthropic", messages: list[dict]):
    common = dict(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
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


def run_agent(user_request: str, verbose: bool = True) -> str:
    client = anthropic.Anthropic()  # liest ANTHROPIC_API_KEY aus der Umgebung
    messages: list[dict] = [{"role": "user", "content": user_request}]
    for iteration in range(1, MAX_ITERATIONS + 1):
        if verbose:
            print(f"\n── Iteration {iteration}/{MAX_ITERATIONS} ──")
        try:
            response = _create_message(client, messages)
        except anthropic.APIError as exc:
            return f"[Abbruch] API-Fehler: {exc}"
        if verbose and getattr(response, "model", MODEL) != MODEL:
            print(f"  (Fallback-Modell hat geantwortet: {response.model})")
        if response.stop_reason == "refusal":
            return "[Abbruch] Anfrage wurde aus Sicherheitsgründen abgelehnt."
        if response.stop_reason != "tool_use":
            final = "".join(b.text for b in response.content if b.type == "text")
            return final.strip() or "[Ende] Keine Textantwort erzeugt."
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if verbose:
                print(f"  → Tool: {block.name}  Input: {json.dumps(block.input, ensure_ascii=False)[:200]}")
            result_text, is_error = _execute_tool(block.name, block.input)
            if verbose:
                preview = result_text if len(result_text) <= 200 else result_text[:200] + "…"
                print(f"    {'✗' if is_error else '✓'} {preview}")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": result_text, "is_error": is_error})
        messages.append({"role": "user", "content": tool_results})
    return (f"[Abbruch] Iterationslimit ({MAX_ITERATIONS}) erreicht, ohne die "
            "Aufgabe abzuschließen. Bitte die Anfrage konkretisieren oder aufteilen.")


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
    if not ensure_api_key():
        print("Ohne API-Key kann der Agent nicht starten.")
        input("Enter zum Schließen ...")
        return
    print("Arbeitsordner:", WORK_DIR)
    print("Stell deine Frage. Leere Eingabe oder 'exit' beendet.\n")
    while True:
        try:
            request = input("Du > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not request or request.lower() in {"exit", "quit"}:
            break
        try:
            answer = run_agent(request)
        except Exception as exc:  # nichts soll das Fenster hart abstürzen lassen
            answer = f"[Fehler] {type(exc).__name__}: {exc}"
        print("\nAgent >", answer, "\n")
    input("Enter zum Schließen ...")


if __name__ == "__main__":
    main()
