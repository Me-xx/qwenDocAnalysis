# Taskplaner-Einrichtung (Windows)

Der Analyzer läuft als geplanter Task: **Bei Anmeldung** starten, Wiederholung
alle 60 Minuten für 12 Stunden, Zwangslaufzeit 2 Stunden. Solange der Desktop-PC
läuft, wird die Queue damit abgearbeitet.

## Voraussetzungen

- Python-Umgebung mit allen Abhängigkeiten installiert
  (`pip install -r requirements.txt`), inkl. Poppler für PDF-Seiten
  (siehe README, Abschnitt Installation).
- `.env` mit `PAPERLESS_TOKEN` liegt im Workspace.
- Falls der Task ein eigenes Venv-Python nutzen soll: Zeile in
  `run_qwen_doc_analyzer.bat` anpassen oder Umgebungsvariable
  `PYTHON_EXE` im Task setzen.

## Variante A: Import der fertigen XML-Definition (empfohlen)

```bat
schtasks /Create /TN "QwenDocAnalyzer" /XML "C:\MeineDateienDesk\qwenDocAnalysis\qwen_doc_analyzer_task.xml"
```

Die Datei `qwen_doc_analyzer_task.xml` enthält bereits: Logon-Trigger mit
Wiederholung PT1H für PT12H, `ExecutionTimeLimit` PT2H, kein
`RunOnlyIfIdle`, kein Akku-Ausschluss, `IgnoreNew` als
Mehrfachinstanz-Richtlinie.

Anschließend prüfen:

```bat
schtasks /Query /TN "QwenDocAnalyzer" /V /FO LIST
```

Löschen (falls nötig):

```bat
schtasks /Delete /TN "QwenDocAnalyzer" /F
```

## Variante B: Minimal-Task per Kommandozeile, Wiederholung per GUI

Einfacher Task ohne Wiederholung (GUI-Schritte unten ergänzen):

```bat
schtasks /Create /TN "QwenDocAnalyzer" ^
  /TR "C:\MeineDateienDesk\qwenDocAnalysis\run_qwen_doc_analyzer.bat" ^
  /SC ONLOGON /RL LIMITED /F
```

Wiederholung lässt sich mit `schtasks` bei `ONLOGON` nicht in einem Befehl
setzen, daher in der GUI nachpflegen:

1. `taskschd.msc` öffnen (Win+R → taskschd.msc).
2. Task "QwenDocAnalyzer" → Eigenschaften.
3. Reiter **Trigger**: "Bei Anmeldung" → Bearbeiten → **Wiederholen: jede
   1 Stunde, für die Dauer von 12 Stunden**.
4. Reiter **Einstellungen**:
   - "Aufgabe beenden, wenn diese länger ausgeführt wird als": **2 Stunden**.
   - Haken bei "Starten nur, wenn sich der Computer im Leerlauf befindet"
     **nicht** setzen.
   - Haken bei "Aufgabe beenden, wenn der Computer im Akkubetrieb läuft" **nicht**
     setzen (Desktop-PC, irrelevant, aber sauber).
   - "Eine neue Instanz nicht starten" (kein Parallelbetrieb).
5. Reiter **Allgemein**: "Unabhängig von der Benutzeranmeldung ausführen" ist
   nicht nötig; der Logon-Trigger reicht, damit der Task beim PC-Start läuft.

## Testlauf von Hand

```bat
C:\MeineDateienDesk\qwenDocAnalysis\run_qwen_doc_analyzer.bat --dry-run --verbose
```

## Verhalten im Betrieb

- Der Task beendet sich nach dem Zeitbudget (`--max-minutes`, Standard 30)
  von selbst sauber; der Taskplaner ruft ihn stündlich erneut auf.
- Die Zwangslaufzeit von 2 Stunden greift nur, falls ein Lauf hängt.
- Ein Parallelstart wird unterbunden (`IgnoreNew`), ein versehentlich
  gleichzeitiger Start ist wegen des `llm_processing`-Tags zusätzlich harmlos.
