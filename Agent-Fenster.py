#!/usr/bin/env python3
"""
Fenster-Oberfläche für den Recherche-Agenten (Eingabefeld statt Konsole).

Start:
  • Windows: Fenster-starten-Windows.vbs doppelklicken (ohne Konsole),
             oder diese Datei direkt doppelklicken.
  • Mac:     Fenster-starten-Mac.command doppelklicken.
  • Linux:   python3 "Agent-Fenster.py"  (ggf. sudo apt install python3-tk)

Braucht agent.py im selben Ordner und das Paket anthropic. Der API-Key wird
beim ersten Start abgefragt.

Neu:
  • Die Antwort erscheint LIVE Wort für Wort (Streaming).
  • "Stoppen" bricht eine laufende Anfrage sauber ab.
  • "Verlauf speichern" schreibt das Gespräch als Textdatei.
  • Gesprächsgedächtnis; "Neu" startet frisch; "Einstellungen" für Such-Keys.
"""

from __future__ import annotations

import datetime
import os
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, simpledialog, messagebox, ttk

import agent  # der Agent-Kern


def ensure_key_gui(root: tk.Tk) -> bool:
    """API-Key sicherstellen: Umgebung -> Datei -> Dialog (wird gespeichert)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if agent._KEY_FILE.exists():
        key = agent._KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
    key = simpledialog.askstring(
        "API-Key benötigt",
        "Bitte Anthropic API-Key eingeben (beginnt mit sk-ant-).\n"
        "Kostenlos unter console.anthropic.com → Settings → API Keys.",
        parent=root, show="*")
    if not key or not key.strip():
        return False
    key = key.strip()
    os.environ["ANTHROPIC_API_KEY"] = key
    try:
        agent._KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(agent._KEY_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return True


class AgentGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel = False
        self.agent = agent.Agent()  # Gespräch MIT Gedächtnis

        # Zustand für die Live-Antwort-Darstellung
        self._answer_open = False
        self._live_answer = ""

        root.title(f"Recherche-Agent — {agent.MODEL}")
        root.geometry("900x640")
        root.minsize(560, 400)

        # obere Leiste mit Aktions-Buttons
        top = tk.Frame(root)
        top.pack(fill="x", padx=10, pady=(10, 0))
        tk.Button(top, text="Neu", width=8, command=self.on_reset).pack(side="left")
        tk.Button(top, text="Verlauf speichern", command=self.on_save).pack(side="left", padx=(6, 0))
        tk.Button(top, text="Einstellungen", command=self.open_settings).pack(side="left", padx=(6, 0))
        self.status = tk.Label(top, text="bereit", fg="#2e7d32")
        self.status.pack(side="right")

        self.out = scrolledtext.ScrolledText(root, wrap="word", state="disabled",
                                             font=("TkDefaultFont", 11), padx=6, pady=6)
        self.out.pack(fill="both", expand=True, padx=10, pady=(8, 6))
        self.out.tag_config("du", foreground="#1565c0", font=("TkDefaultFont", 11, "bold"))
        self.out.tag_config("agent", foreground="#1b5e20")
        self.out.tag_config("schritt", foreground="#8a8a8a")

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self.entry = tk.Entry(bar, font=("TkDefaultFont", 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _e: self.on_send())
        self.entry.focus()
        self.btn = tk.Button(bar, text="Senden", width=10, command=self.on_send)
        self.btn.pack(side="left", padx=(8, 0))
        self.stop_btn = tk.Button(bar, text="Stoppen", width=9,
                                  command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        self._log(f"Bereit. Frage eingeben und Enter drücken. Folgefragen bauen "
                  f"aufeinander auf; die Antwort erscheint live.\n"
                  f"Arbeitsordner: {agent.WORK_DIR}", "schritt")
        self.root.after(60, self._drain)

    # ---- Ausgabe-Helfer ----
    def _raw(self, text: str, tag: str | None = None) -> None:
        self.out.configure(state="normal")
        self.out.insert("end", text, tag)
        self.out.see("end")
        self.out.configure(state="disabled")

    def _log(self, text: str, tag: str | None = None) -> None:
        self._raw(text + "\n", tag)

    def _begin_answer(self) -> None:
        self._raw("\nAgent > ", "agent")
        self._answer_open = True
        self._live_answer = ""

    def _close_answer(self) -> None:
        if self._answer_open:
            self._raw("\n")
            self._answer_open = False

    # ---- Aktionen ----
    def on_reset(self) -> None:
        if self.busy:
            return
        self.agent.reset()
        self._log("\n— neues Gespräch —", "schritt")

    def on_save(self) -> None:
        text = self.out.get("1.0", "end").strip()
        if not text:
            return
        name = "verlauf_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
        path = agent.WORK_DIR / name
        try:
            path.write_text(text, encoding="utf-8")
            self._log(f"Verlauf gespeichert: {path}", "schritt")
        except OSError as exc:
            self._log(f"Konnte Verlauf nicht speichern: {exc}", "schritt")

    def on_stop(self) -> None:
        if self.busy:
            self.cancel = True
            self.status.configure(text="wird gestoppt …", fg="#b26a00")

    def on_send(self) -> None:
        if self.busy:
            return
        req = self.entry.get().strip()
        if not req:
            return
        self.entry.delete(0, "end")
        self._log("\nDu > " + req, "du")
        self.busy = True
        self.cancel = False
        self.btn.configure(state="disabled", text="…")
        self.stop_btn.configure(state="normal")
        self.status.configure(text="arbeitet …", fg="#b26a00")
        threading.Thread(target=self._work, args=(req,), daemon=True).start()

    def _work(self, req: str) -> None:
        try:
            answer = self.agent.ask(
                req, verbose=False,
                on_step=lambda s: self.q.put(("step", s)),
                on_delta=lambda t: self.q.put(("delta", t)),
                should_cancel=lambda: self.cancel,
            )
        except Exception as exc:  # nichts soll das Fenster abstürzen lassen
            answer = f"[Fehler] {type(exc).__name__}: {exc}"
        self.q.put(("done", answer))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "step":
                    self._close_answer()
                    self._log("   " + payload, "schritt")
                elif kind == "delta":
                    if not self._answer_open:
                        self._begin_answer()
                    self._raw(payload, "agent")
                    self._live_answer += payload
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(60, self._drain)

    def _finish(self, answer: str) -> None:
        # Wurde die Antwort bereits live gestreamt? Dann nicht doppelt ausgeben.
        already = self._answer_open and answer.strip() == self._live_answer.strip()
        self._close_answer()
        if not already:
            self._log("\nAgent > " + answer, "agent")
        self.busy = False
        self.cancel = False
        self.btn.configure(state="normal", text="Senden")
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="bereit", fg="#2e7d32")
        self.entry.focus()

    def open_settings(self) -> None:
        if self.busy:
            self._log("Bitte warten, bis die aktuelle Anfrage fertig ist.", "schritt")
            return
        win = tk.Toplevel(self.root)
        win.title("Einstellungen")
        win.transient(self.root)
        win.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        tk.Label(win, text="Optionale Such-Schlüssel (leer lassen = schlüssellose "
                           "DuckDuckGo-Suche):").grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(win, text="Tavily API-Key:").grid(row=1, column=0, sticky="e", **pad)
        tav = tk.Entry(win, width=44, show="*")
        tav.grid(row=1, column=1, **pad)
        tav.insert(0, os.environ.get("TAVILY_API_KEY", ""))
        tk.Label(win, text="Brave API-Key:").grid(row=2, column=0, sticky="e", **pad)
        bra = tk.Entry(win, width=44, show="*")
        bra.grid(row=2, column=1, **pad)
        bra.insert(0, os.environ.get("BRAVE_API_KEY", ""))

        tk.Label(win, text="Gründlichkeit (Effort):").grid(row=3, column=0, sticky="e", **pad)
        eff = ttk.Combobox(win, values=["low", "medium", "high", "xhigh", "max"],
                           state="readonly", width=10)
        eff.set(agent.EFFORT)
        eff.grid(row=3, column=1, sticky="w", **pad)

        def save():
            agent.save_config({
                "TAVILY_API_KEY": tav.get().strip(),
                "BRAVE_API_KEY": bra.get().strip(),
                "effort": eff.get(),
            })
            self._log("Einstellungen gespeichert.", "schritt")
            win.destroy()

        tk.Button(win, text="Speichern", command=save).grid(row=4, column=1, sticky="e", **pad)


def main() -> None:
    agent.load_config()
    root = tk.Tk()
    if not ensure_key_gui(root):
        messagebox.showinfo("Kein API-Key",
                            "Ohne API-Key kann der Agent nicht starten.")
        root.destroy()
        return
    AgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
