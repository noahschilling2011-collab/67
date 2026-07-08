#!/bin/bash
# Starter fuer Linux. Doppelklick (ggf. "Ausfuehren" waehlen) oder im
# Terminal:  ./Agent-starten-Linux.sh

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python 3 ist nicht installiert. Beispiel (Debian/Ubuntu):"
    echo "  sudo apt install python3 python3-pip"
    echo
    read -n1 -r -p "Taste druecken zum Schliessen ..."
    exit 1
fi

echo "Installiere/pruefe benoetigte Pakete ..."
python3 -m pip install --quiet anthropic

echo
python3 agent.py

echo
read -n1 -r -p "Fertig. Taste druecken zum Schliessen ..."
