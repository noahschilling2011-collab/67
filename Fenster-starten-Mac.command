#!/bin/bash
# Startet die Fenster-Oberflaeche (Agent-Fenster.py) per Doppelklick.
# Beim ersten Mal ggf. Rechtsklick -> "Oeffnen" (macOS Gatekeeper).
#
# Hinweis: macOS oeffnet dazu kurz ein Terminal-Fenster; das eigentliche
# Programm laeuft im eigenen Fenster und das Terminal kann geschlossen werden.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 ist nicht installiert: https://www.python.org/downloads/"
    read -n1 -r -p "Taste druecken zum Schliessen ..."
    exit 1
fi

# anthropic sicherstellen (leise), dann GUI im Hintergrund starten
python3 -m pip install --quiet anthropic >/dev/null 2>&1
python3 "Agent-Fenster.py" >/dev/null 2>&1 &

# Terminal darf sich schliessen; die GUI laeuft weiter.
