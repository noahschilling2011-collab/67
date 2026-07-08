"""
Phase 2 — Voice-Layer (nur Wrapper um den fertigen Agent-Kern).

WICHTIG: Diese Datei ruft ausschließlich run_agent() aus agent.py auf und
ändert den Agent-Kern nicht. Sprache -> Text -> Agent -> Text -> Sprache.

Erst benutzen, wenn Phase 1 (agent.py) im Terminal läuft.

────────────────────────────────────────────────────────────────────────────
Installation
────────────────────────────────────────────────────────────────────────────
    pip install SpeechRecognition pyttsx3

    Mikrofon-Zugriff (PyAudio) je nach System:
        Linux:   sudo apt install portaudio19-dev && pip install pyaudio
        macOS:   brew install portaudio && pip install pyaudio
        Windows: pip install pyaudio

Bibliotheken:
    - SpeechRecognition  : STT. Standardmäßig recognize_google (kostenlos,
                           ohne Key, braucht Internet). Offline-Alternative:
                           pip install openai-whisper  -> recognize_whisper.
    - pyttsx3            : TTS, komplett offline, kein Key, plattformübergreifend.

Der ANTHROPIC_API_KEY wird weiterhin nur als Umgebungsvariable erwartet
(siehe agent.py) — hier steht kein Key.

Start:
    python voice.py
"""

from __future__ import annotations

import speech_recognition as sr
import pyttsx3

from agent import run_agent, ensure_api_key  # <- der unveränderte Agent-Kern aus Phase 1

# STT-Sprache/Region. "de-DE" für Deutsch, "en-US" für Englisch.
STT_LANGUAGE = "de-DE"
MIN_TRANSCRIPT_CHARS = 2  # kürzere Erkennungen gelten als Rauschen


# ─────────────────────────────────────────────────────────────────────────────
# Text -> Sprache
# ─────────────────────────────────────────────────────────────────────────────

_tts = pyttsx3.init()


def speak(text: str) -> None:
    print("Agent >", text)
    _tts.say(text)
    _tts.runAndWait()


# ─────────────────────────────────────────────────────────────────────────────
# Sprache -> Text  (inkl. Behandlung von Fehl-/Nicht-Erkennung)
# ─────────────────────────────────────────────────────────────────────────────

def listen(recognizer: sr.Recognizer, mic: sr.Microphone) -> str | None:
    """Nimmt einmal auf und gibt den erkannten Text zurück.

    Rückgabe:
        str  -> erkannter Text
        None -> nichts Verwertbares erkannt (Rauschen, Timeout, Dienstfehler).
                Der Aufrufer bittet dann um Wiederholung, statt Unsinn an den
                Agenten zu schicken.
    """
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.4)
        print("(zuhören …)")
        try:
            audio = recognizer.listen(source, timeout=6, phrase_time_limit=15)
        except sr.WaitTimeoutError:
            return None  # nichts gesagt

    try:
        text = recognizer.recognize_google(audio, language=STT_LANGUAGE)
    except sr.UnknownValueError:
        return None  # Audio kam an, war aber unverständlich
    except sr.RequestError as exc:
        # STT-Dienst nicht erreichbar (kein Netz o. Ä.) — kein Crash.
        print(f"[STT] Dienst nicht erreichbar: {exc}")
        return None

    text = text.strip()
    # Fehlerkennung abfangen: zu kurze Fragmente sind fast immer Rauschen.
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return None
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Sprach-Schleife
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not ensure_api_key():
        print("Ohne API-Key kann der Agent nicht starten.")
        return

    recognizer = sr.Recognizer()
    try:
        mic = sr.Microphone()
    except OSError as exc:
        print(f"[Fehler] Kein Mikrofon verfügbar: {exc}")
        return

    speak("Sprach-Agent bereit. Sag deine Anfrage. 'Stopp' beendet.")
    misses = 0

    while True:
        transcript = listen(recognizer, mic)

        # --- Fall: Spracherkennung versteht Unsinn / nichts ---
        if transcript is None:
            misses += 1
            if misses >= 3:
                speak("Ich verstehe dich gerade nicht. Ich beende lieber.")
                break
            speak("Das habe ich nicht verstanden. Bitte wiederhole das.")
            continue
        misses = 0

        print("Du (erkannt) >", transcript)

        if transcript.lower() in {"stopp", "stop", "beenden", "exit", "ende"}:
            speak("Auf Wiedersehen.")
            break

        # Bestätigungs-Gate: zurücksprechen, was verstanden wurde. Das fängt
        # falsch erkannte Anfragen ab, BEVOR der Agent (und damit Tools/Dateien)
        # darauf reagiert. Für kritische Aktionen ist das die wichtigste Bremse.
        speak(f"Ich habe verstanden: {transcript}. Sage ja zum Ausführen.")
        confirm = listen(recognizer, mic)
        if confirm is None or confirm.lower() not in {"ja", "jawohl", "ok", "yes"}:
            speak("Abgebrochen. Bitte neu formulieren.")
            continue

        # --- Fertiger Agent-Kern, unverändert aufgerufen ---
        answer = run_agent(transcript, verbose=False)
        speak(answer)


if __name__ == "__main__":
    main()
