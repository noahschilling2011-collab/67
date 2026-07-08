#!/bin/bash
# Doppelklick-Starter fuer macOS.
# Beim ersten Mal ggf. Rechtsklick -> "Oeffnen" waehlen (Gatekeeper).

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python 3 ist nicht installiert."
    echo "Installiere es von https://www.python.org/downloads/ und starte erneut."
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
