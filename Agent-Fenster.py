#!/usr/bin/env python3
"""
Fenster-Oberfläche für den Recherche-Agenten (Eingabefeld statt Konsole).

Start:
  • Windows: diese Datei doppelklicken.
  • Mac/Linux: Terminal →  python3 "Agent-Fenster.py"
    (Linux braucht evtl. einmal:  sudo apt install python3-tk)

Braucht die Datei agent.py im selben Ordner (der Agent-Kern) und das Paket
anthropic (pip install anthropic). Der API-Key wird beim ersten Start in einem
kleinen Dialog abgefragt und lokal gespeichert.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, simpledialog, messagebox

import agent  # der unveränderte Agent-Kern


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

        root.title(f"Recherche-Agent — {agent.MODEL}")
        root.geometry("780x580")

        self.out = scrolledtext.ScrolledText(root, wrap="word", state="disabled")
        self.out.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.out.tag_config("du", foreground="#1565c0")
        self.out.tag_config("agent", foreground="#2e7d32")
        self.out.tag_config("schritt", foreground="#777777")

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.entry = tk.Entry(bar, font=("TkDefaultFont", 12))
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self.on_send())
        self.entry.focus()
        self.btn = tk.Button(bar, text="Senden", width=10, command=self.on_send)
        self.btn.pack(side="left", padx=(6, 0))

        self._log(f"Bereit. Frage eingeben und Enter drücken.\n"
                  f"Arbeitsordner: {agent.WORK_DIR}", "schritt")
        self.root.after(100, self._drain)

    def _log(self, text: str, tag: str | None = None) -> None:
        self.out.configure(state="normal")
        self.out.insert("end", text + "\n", tag)
        self.out.see("end")
        self.out.configure(state="disabled")

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
        threading.Thread(target=self._work, args=(req,), daemon=True).start()

    def _work(self, req: str) -> None:
        try:
            answer = agent.run_agent(req, verbose=False, on_step=self.q.put)
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
                    self.entry.focus()
                else:
                    self._log("   " + str(item), "schritt")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)


def main() -> None:
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
