#!/usr/bin/env python3
"""
Fenster-Oberfläche für den Recherche-Agenten (Eingabefeld statt Konsole).

Start:
  • Windows: Fenster-starten-Windows.vbs doppelklicken (ohne Konsole),
             oder diese Datei direkt doppelklicken.
  • Mac:     Fenster-starten-Mac.command doppelklicken.
  • Linux:   python3 "Agent-Fenster.py"  (ggf. sudo apt install python3-tk)

Braucht agent.py im selben Ordner und das Paket anthropic. Der API-Key wird
beim ersten Start abgefragt. Gesprächsgedächtnis ist aktiv (Folgefragen bauen
aufeinander auf) — "Neu" startet ein frisches Gespräch. Über "Einstellungen"
lassen sich optionale Such-Schlüssel (Tavily/Brave) und die Gründlichkeit setzen.
"""

from __future__ import annotations

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
        self.agent = agent.Agent()  # Gespräch MIT Gedächtnis

        root.title(f"Recherche-Agent — {agent.MODEL}")
        root.geometry("800x600")

        # obere Leiste mit Aktions-Buttons
        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=(8, 0))
        tk.Button(top, text="Neu (Gespräch)", command=self.on_reset).pack(side="left")
        tk.Button(top, text="Einstellungen", command=self.open_settings).pack(side="left", padx=(6, 0))
        self.status = tk.Label(top, text="bereit", fg="#777")
        self.status.pack(side="right")

        self.out = scrolledtext.ScrolledText(root, wrap="word", state="disabled")
        self.out.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self.out.tag_config("du", foreground="#1565c0")
        self.out.tag_config("agent", foreground="#2e7d32")
        self.out.tag_config("schritt", foreground="#888888")

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.entry = tk.Entry(bar, font=("TkDefaultFont", 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self.on_send())
        self.entry.focus()
        self.btn = tk.Button(bar, text="Senden", width=10, command=self.on_send)
        self.btn.pack(side="left", padx=(6, 0))

        self._log(f"Bereit. Frage eingeben und Enter drücken. Folgefragen bauen "
                  f"aufeinander auf.\nArbeitsordner: {agent.WORK_DIR}", "schritt")
        self.root.after(100, self._drain)

    def _log(self, text: str, tag: str | None = None) -> None:
        self.out.configure(state="normal")
        self.out.insert("end", text + "\n", tag)
        self.out.see("end")
        self.out.configure(state="disabled")

    def on_reset(self) -> None:
        if self.busy:
            return
        self.agent.reset()
        self._log("\n— neues Gespräch —", "schritt")

    def on_send(self) -> None:
        if self.busy:
            return
        req = self.entry.get().strip()
        if not req:
            return
        self.entry.delete(0, "end")
        self._log("\nDu > " + req, "du")
        self.busy = True
        self.btn.configure(state="disabled", text="…")
        self.status.configure(text="arbeitet …")
        threading.Thread(target=self._work, args=(req,), daemon=True).start()

    def _work(self, req: str) -> None:
        try:
            answer = self.agent.ask(req, verbose=False, on_step=self.q.put)
        except Exception as exc:  # nichts soll das Fenster abstürzen lassen
            answer = f"[Fehler] {type(exc).__name__}: {exc}"
        self.q.put(("__DONE__", answer))

    def _drain(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__DONE__":
                    self._log("\nAgent > " + item[1], "agent")
                    self.busy = False
                    self.btn.configure(state="normal", text="Senden")
                    self.status.configure(text="bereit")
                    self.entry.focus()
                else:
                    self._log("   " + str(item), "schritt")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def open_settings(self) -> None:
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
