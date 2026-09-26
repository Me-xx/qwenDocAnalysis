# Qwen-Doc-Analyzer

Windows-Queue-Worker für Paperless-ngx: Dokumente mit dem Tag `llm_pending`
werden per REST-API abgerufen, lokal mit **Qwen2-VL** (INT4/NF4 via bitsandbytes)
analysiert und das Ergebnis (Korrespondent, Dokumenttyp, Titel, Datum,
Zusammenfassung, Tags) zurückgeschrieben. Der komplette Zustand liegt in
Paperless selbst — es gibt keinen lokalen Queue-State.

Queue-Tags: `llm_pending` (wartet) → `llm_processing` (in Bearbeitung) →
`llm_done` (fertig) bzw. `llm_error` (fehlgeschlagen).

## Modulstruktur

| Datei | Aufgabe |
|---|---|
| `qwen_doc_analyzer.py` | Einstiegspunkt (ruft `main.main()` auf) |
| `main.py` | CLI, Logging, Queue-Loop, Budgets, Tag-Übergänge, Pullback-Regeln |
| `api.py` | Paperless-REST-Client (Token-Auth, Retry/Backoff, Timeout 30 s) |
| `analyzer.py` | Qwen2-VL: Laden (NF4), PDF-Seitenextraktion, JSON-Prompt, Validierung |
| `run_qwen_doc_analyzer.bat` | Wrapper für den Taskplaner |
| `qwen_doc_analyzer_task.xml` | Fertige Taskplaner-Definition (siehe `task_setup.md`) |

## Installation

```bat
cd C:\MeineDateienDesk\qwenDocAnalysis
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Für PDF-Seiten wird **Poppler** benötigt (nur das Binary, kein Python-Paket):

1. Windows-Build herunterladen, z. B. von
   https://github.com/oschwartz10612/poppler-windows/releases (Release/…/bin).
2. Ordner entpacken, z. B. nach `C:\Tools\poppler-24.08.0\`.
3. Entweder `…\bin` in den System-PATH aufnehmen oder den Pfad beim Aufruf
   übergeben: `--poppler-path "C:\Tools\poppler-24.08.0\Library\bin"`.

Das Modell Qwen2-VL wird aus dem lokalen Verzeichnis geladen (Standard:
`C:\MeineDateienDesk\MCP\models\Qwen2VL`), keine HuggingFace-Downloads nötig.

## Konfiguration

```bat
copy .env.example .env
```

In `.env` den Token aus der Paperless-Web-GUI
(Einstellungen → API-Authentifizierung) eintragen:

```
PAPERLESS_TOKEN=<dein-token>
PAPERLESS_URL=http://192.168.178.137:8000
```

Alternativ: `--token` bzw. `--url` als Argument oder als Umgebungsvariablen.
Token wird nirgends hartcodiert; `.env` ist per `.gitignore` ausgenommen.

## Erste Ausführung

```bat
venv\Scripts\python.exe qwen_doc_analyzer.py --dry-run --verbose
```

Zeigt alle Dokumente mit Tag `llm_pending`, ohne etwas zu verändern. Danach
einzelne Dokumente in Paperless mit `llm_pending` taggen und den echten Lauf
starten:

```bat
venv\Scripts\python.exe qwen_doc_analyzer.py --max-docs 1 --verbose --create-entities
```

Nützliche Optionen: `--overwrite` (vorhandene Felder überschreiben),
`--summary note` (Zusammenfassung als Notiz statt Custom Field),
`--max-minutes` / `--max-docs` (Budgets), `--poppler-path`.

Für den Dauerbetrieb als geplanter Task siehe **`task_setup.md`**.

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Lauf sauber beendet (Queue leer, Budget erschöpft oder Ctrl+C) |
| 1 | Fehler, z. B. kein Token, Paperless unerreichbar, Modell-Ladefehler, 3 Dokumente in Folge fehlgeschlagen |
| 2 | Keine CUDA-fähige GPU gefunden |

## Fehlersuche

- **HTTP 401 / AUTH**: Token falsch oder abgelaufen → neuen Token erzeugen.
- **Exit 2**: CUDA-Treiber prüfen (`nvidia-smi`); Modell ist NF4-quantisiert und
  braucht eine CUDA-GPU.
- **`pdf2image`-Fehler / leere Seitenliste**: Poppler fehlt oder
  `--poppler-path` zeigt nicht auf das `bin`-Verzeichnis.
- **Dokumente landen auf `llm_error`**: Log unter `qwen_doc_analyzer.log`
  prüfen — meist ungültige Modell-JSON oder Downloadfehler. Das Dokument
  behält seinen Inhalt; Tag in Paperless von `llm_error` auf `llm_pending`
  zurücksetzen, um es erneut zu probieren.
- **Hängender Lauf abgebrochen**: Dokumente mit `llm_processing` länger als
  60 Minuten werden beim nächsten Start automatisch zurück auf `llm_pending`
  gesetzt (Crash-Recovery).
- Logging: Datei `qwen_doc_analyzer.log` plus Konsole; `--verbose` schaltet
  auf Debug-Level.
