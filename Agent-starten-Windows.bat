@echo off
REM Doppelklick-Starter fuer Windows.
REM Wechselt in den Ordner dieser Datei, installiert bei Bedarf das
REM anthropic-Paket und startet den Agenten.

cd /d "%~dp0"
title Autonomer Recherche-Agent

REM Python finden (py-Launcher bevorzugt, sonst python)
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo.
        echo Python ist nicht installiert.
        echo Bitte von https://www.python.org/downloads/ installieren
        echo und beim Setup "Add Python to PATH" anhaken.
        echo.
        pause
        exit /b 1
    )
)

echo Installiere/pruefe benoetigte Pakete ...
%PY% -m pip install --quiet --disable-pip-version-check anthropic

echo.
%PY% agent.py

echo.
pause
