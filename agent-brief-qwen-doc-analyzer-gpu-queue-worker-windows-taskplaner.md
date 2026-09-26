# Agent-Brief: Qwen-Doc-Analyzer — Python-Worker für die Paperless-GPU-Queue

## Rolle & Auftrag
Du bist ein Python-Agent auf dem **Windows-Desktoprechner** (Arbeitsumgebung VS Code für Windows). Entwickle ein produktionsreifes Python-Script `qwen_doc_analyzer.py`, das als **geplanter Windows-Task** läuft, wartende Paperless-Dokumente per REST-API abruft, mit Qwen-VL lokal analysiert und die Ergebnisse per API zurückschreibt. Repo: `github.com/Me-xx/MCP.git` (gleiche Struktur wie `qwen_tagger.py`).

## Kontext / vorhandene Infrastruktur
- Paperless-ngx läuft im LXC 110: `http://192.168.178.137:8000`, REST-API mit Token (in Paperless-Web-GUI erzeugt)
- Queue-Marker: Tag **`llm_pending`** (zu analysieren), **`llm_done`** (fertig), optional **`llm_error`**
- Lokales Modell: Qwen2-VL-7B-Instruct INT4/NF4 mit bitsandbytes unter `C:\MeineDateienDesk\MCP\models\Qwen2VL` (aus `qwen_tagger.py` übernehmen: Loader, NF4-Config, Decoder)
- Bekannte Fehler im bestehenden Code NICHT übernehmen: toter Profiler-Benchmark-Code im `__main__`-Block, invertierte `--recalc_all`-Flaglogik, `if True:`-Dekodierungszweig, `torch.compile` auf 4-bit-Modell mit dynamischen Bildgrößen weglassen.

## Architektur
```
Geplanter Task (Taskplaner, bei Anmeldung / stündlich, StopAfterDuration)
  └─ qwen_doc_analyzer.py
       1. API: GET /api/documents/?tags__name__iexact=llm_pending&truncate_content=false
       2. Pro Dokument:
          a) GET /api/documents/{id}/download/  (Original-PDF/Bild)
          b) Bildseite(n) extrahieren (pdf2image/poppler, max 3 Seiten, resize auf ~1280px Langseite)
          c) Qwen-VL-Prompt: strukturierter JSON-Extract (s. Prompt-Schema)
          d) API PATCH /api/documents/{id}/: correspondent, document_type, title, tags, (custom_field Zusammenfassung wenn eingerichtet)
          e) Tag-Übergang llm_pending → llm_done (bzw. llm_error)
       3. GPU frei? → nächstes Dokument; Ende der Queue oder Budget erschöpft → sauber beenden
```

## Anforderungen

### 1. Robustes Queue-Handling (State nur in Paperless!)
- **Kein lokaler State**: Die Queue ist der Tag `llm_pending` in Paperless. Absturz/Shutdown verliert nichts.
- **Bounded Runtime**: Parameter `--max-minutes N` (Default 30) und `--max-docs N` (Default 50), danach sauberes Beenden — der Taskplaner ruft später wieder.
- **Reserve-Lock**: Vor Verarbeitungsbeginn Tag `llm_processing` setzen (Atomic, über PATCH). Beim Start Dokumente mit `llm_processing` älter als 60 Min auf `llm_pending` zurücksetzen (Crash-Recovery).
- Jedes Dokument einzeln committen — ein Fehler bricht nicht den Batch ab (`llm_error` + Logging, weiter mit nächstem).

### 2. API-Client
- `requests`, Token-Auth via Env-Var `PAPERLESS_TOKEN` oder `--token`/Configdatei `.env` (nicht hartcodiert)
- Basis-URL konfigurierbar: `--url http://192.168.178.137:8000`
- Korrespondenten/Dokumenttypen per `/api/correspondents/` bzw. `/api/document_types/` auflösen (ggf. automatisch anlegen, Flag `--create-entities`)
- Retry mit Backoff bei 5xx/Netzwerkfehlern (3 Versuche), Timeout 30 s

### 3. Analyse (Qwen-VL)
- Prompt: Dokument als Bild + Systeminstruktion, Antwort **strikt als JSON**:
```json
{
  "correspondent": "Firmen- oder Behördenname",
  "document_type": "Rechnung|Behördenschreiben|Bedienungsanleitung|Lieferschein|Vertrag|Sonstiges",
  "title": "kurzer aussagekräftiger Titel",
  "date": "YYYY-MM-DD oder null",
  "summary": "1-2 Sätze Inhalt",
  "tags": ["optionale", "Schlüsselworte"]
}
```
- JSON-Ausgabe validieren (try/except + Fallback-Reparatur, z. B. Regex-Extraktion des ersten `{...}`-Blocks); bei ungültiger Antwort: `llm_error`, nicht crashen
- `document_type` nur aus fester Whitelist wählen; unbekannt → „Sonstiges"
- Länge der Bildkante begrenzen (VRAM-Schutz NF4), `torch.cuda`-Verfügbarkeit prüfen, ohne CUDA sofort sauber mit Exit-Code 2 beenden
- Absender/Empfänger-Logik: `correspondent` = Absender; Rückseite/CC-Empfänger ignorieren

### 4. Rück-Schreiben („Pullback")
- `PATCH /api/documents/{id}/` mit den analysierten Feldern
- **Niemals überschreiben**, was ein Mensch schon gepflegt hat: Feld nur setzen, wenn im Dokument bisher leer/unbenannt („Sonstiges" + Titel „") — Flag `--overwrite` für bewusstes Überschreiben
- Zusammenfassung: Custom Field „Zusammenfassung" anlegen (falls fehlt) und füllen; andernfalls als Kommentar anfügen (`POST /api/documents/{id}/notes/`)
- Datum: `created` in ISO-Format mitsetzen, wenn Qwen ein Datum liefert und keins erkannt wurde
- Tag-Übergang nach erfolgreichem PATCH; erst danach `llm_pending` entfernen

### 5. Windows Taskplaner-Einbindung
- `.bat`-Wrapper oder direkter `python.exe`-Aufruf, Working Directory auf `C:\MeineDateienDesk\MCP` (bzw. Repo-Pfad)
- Trigger: „Bei Anmeldung" (d. h. wenn der Desktop-PC startet) + Wiederholung alle 60 Min für 12 h
- Bedingung: Task nur starten, wenn Rechner an — das erfüllt das Konzept „PC läuft → Queue abarbeiten"
- Einstellungen: Task beenden nach 2 h Zwangslaufzeit; bei Akku n/a; `Start only if idle` NICHT setzen
- Kurzanleitung `task_setup.md` mit `schtasks`-Beispielbefehl und manueller GUI-Variante

### 6. Codequalität
- `logging` in Datei + Konsole (`--verbose`), kein print
- `argparse` mit `--dry-run` (Queue anzeigen, nichts verändern)
- Modulstruktur: `api.py` (Client), `analyzer.py` (Modell), `main.py` — oder ein File, wenn <400 Zeilen
- README-Abschnitt: Installation (`pip install -r requirements.txt`), erste Ausführung, Fehlersuche (Exit-Codes: 0 ok, 1 Fehler, 2 keine CUDA)

## Abnahme / Test
1. `--dry-run` zeigt 2+ Dokumente mit Tag `llm_pending`
2. Ein Testbild-PDF wird analysiert: Korrespondent, Typ, Titel, Zusammenfassung korrekt in Paperless sichtbar
3. Tag-Übergang `llm_pending → llm_done` in der GUI sichtbar
4. Vorzeitig abbrechen (Ctrl+C): laufendes Dokument geht zurück in die Queue (oder wird als `llm_error` markiert und übersprungen)
5. Zweiter Start nach Abschluss: „Queue leer", Exit 0
6. Verbindungsabbruch (Paperless-IP kurz blockieren): Retry, danach `llm_error`, kein Crash

## Abschlussbericht
Struktur der Module, verwendeter Prompt, Exit-Codes, Taskplaner-Befehl, Commits ins MCP-Repo, offene Punkte (z. B. Custom-Fields-ID der Zusammenfassung, gewählte Modellvariante).