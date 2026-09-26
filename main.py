"""Orchestrierung: Queue abarbeiten, Budgets, Tag-Übergänge, CLI, Logging."""

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analyzer import AnalyzerError, QwenAnalyzer
from api import PaperlessAPIError, PaperlessClient

log = logging.getLogger("qwen_doc_analyzer")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_CUDA = 2

TAG_PENDING = "llm_pending"
TAG_DONE = "llm_done"
TAG_ERROR = "llm_error"
TAG_PROCESSING = "llm_processing"

SUMMARY_FIELD_NAME = "Zusammenfassung"

# Nach so vielen Dokumenten in Folge ohne Fortschritt wird der Lauf beendet
# (schützt z. B. gegen einen kompletten API-Ausfall gegen Ende des Budgets).
MAX_CONSECUTIVE_ERRORS = 3

DEFAULT_URL = "http://192.168.178.137:8000"
DEFAULT_MODEL_PATH = r"C:\MeineDateienDesk\MCP\models\Qwen2VL"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Paperless-Queue-Worker: verarbeitet Dokumente mit dem Tag "
                    "'llm_pending' lokal mit Qwen2-VL und schreibt die "
                    "Analyse zurück.",
        epilog="Exit-Codes: 0 = ok, 1 = Fehler, 2 = keine CUDA-fähige GPU")
    parser.add_argument("--url", default=None,
                        help="Paperless-Basis-URL (Standard: Env PAPERLESS_URL "
                             f"oder {DEFAULT_URL})")
    parser.add_argument("--token", default=None,
                        help="Paperless-API-Token (Standard: Env PAPERLESS_TOKEN "
                             "oder .env-Datei)")
    parser.add_argument("--env-file", default=".env",
                        help="Pfad zur .env-Datei (Standard: .env im "
                             "Arbeitsverzeichnis)")
    parser.add_argument("--model-path", default=DEFAULT_MODEL_PATH,
                        help="Lokales Qwen2-VL-Modellverzeichnis")
    parser.add_argument("--poppler-path", default=None,
                        help="Pfad zum poppler 'bin'-Verzeichnis (nur für "
                             "PDF-Seitenextraktion nötig)")
    parser.add_argument("--max-minutes", type=int, default=30,
                        help="Zeitbudget in Minuten, danach sauber beenden "
                             "(Standard: 30)")
    parser.add_argument("--max-docs", type=int, default=50,
                        help="Maximale Anzahl Dokumente pro Lauf (Standard: 50)")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="Maximale Anzahl analysierter PDF-Seiten pro "
                             "Dokument (Standard: 3)")
    parser.add_argument("--recovery-minutes", type=int, default=60,
                        help="Dokumente in 'llm_processing', die länger als N "
                             "Minuten hängen, werden zurückgesetzt "
                             "(Standard: 60)")
    parser.add_argument("--summary", choices=["custom_field", "note"],
                        default="custom_field",
                        help="Wohin mit der Zusammenfassung: Custom Field "
                             "'Zusammenfassung' (wird bei Bedarf angelegt) "
                             "oder als Notiz (Standard: custom_field)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Vorhandene Felder bewusst überschreiben "
                             "(Standard: nur leere/unbenannte Felder setzen)")
    parser.add_argument("--create-entities", action="store_true",
                        help="Fehlende Korrespondenten, Dokumenttypen und "
                             "Tags automatisch anlegen")
    parser.add_argument("--dry-run", action="store_true",
                        help="Queue nur anzeigen, nichts verändern")
    parser.add_argument("--verbose", action="store_true",
                        help="Debug-Logging (Konsole und Datei)")
    parser.add_argument("--log-file", default="qwen_doc_analyzer.log",
                        help="Pfad zur Logdatei")
    return parser.parse_args(argv)


def setup_logging(args):
    level = logging.DEBUG if args.verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.FileHandler(args.log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, stream_handler]


def load_env_file(path):
    """KEY=VALUE-Zeilen aus .env laden; vorhandene Env-Vars haben Vorrang."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(),
                               value.strip().strip('"').strip("'"))


def cuda_available():
    import torch
    return torch.cuda.is_available()


def parse_iso_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv=None):
    args = parse_args(argv)
    load_env_file(args.env_file)
    setup_logging(args)

    token = args.token or os.environ.get("PAPERLESS_TOKEN")
    url = args.url or os.environ.get("PAPERLESS_URL") or DEFAULT_URL
    if not token:
        log.error("Kein API-Token gefunden. --token, Umgebungsvariable "
                  "PAPERLESS_TOKEN oder .env-Datei bereitstellen.")
        return EXIT_ERROR

    client = PaperlessClient(url, token)

    try:
        docs = client.list_documents(TAG_PENDING)
    except PaperlessAPIError as exc:
        log.error("Paperless nicht erreichbar (%s): %s", url, exc)
        return EXIT_ERROR

    if args.dry_run:
        if not docs:
            log.info("Dry-Run: Queue leer (0 Dokumente mit Tag '%s').",
                     TAG_PENDING)
        else:
            log.info("Dry-Run: %d Dokument(e) mit Tag '%s':",
                     len(docs), TAG_PENDING)
            for doc in docs:
                log.info("  [%s] %s | Korrespondent: %s | Typ: %s | erstellt: %s",
                         doc["id"], doc.get("title"), doc.get("correspondent"),
                         doc.get("document_type"), doc.get("created"))
        return EXIT_OK

    if not docs:
        log.info("Queue leer - nichts zu tun.")
        return EXIT_OK

    try:
        tag_ids = {
            "pending": client.ensure_tag(TAG_PENDING),
            "done": client.ensure_tag(TAG_DONE),
            "error": client.ensure_tag(TAG_ERROR),
            "processing": client.ensure_tag(TAG_PROCESSING),
        }
        recover_stale_processing(client, args.recovery_minutes, tag_ids)
    except PaperlessAPIError as exc:
        log.error("Tag-Setup oder Crash-Recovery fehlgeschlagen: %s", exc)
        return EXIT_ERROR

    if not cuda_available():
        log.error("Keine CUDA-fähige GPU verfügbar - Analyse nicht möglich.")
        return EXIT_NO_CUDA

    analyzer = QwenAnalyzer(model_path=args.model_path, max_pages=args.max_pages)
    try:
        analyzer.load()
    except AnalyzerError as exc:
        log.error("Modell konnte nicht geladen werden: %s", exc)
        return EXIT_NO_CUDA
    except Exception as exc:
        log.error("Modell konnte nicht geladen werden: %s", exc)
        return EXIT_ERROR

    document_types = {dt["id"]: dt.get("name", "")
                      for dt in client.list_document_types()}

    log.info("Starte Verarbeitung: %d Dokument(e) in der Queue, Budget "
             "%d Min / %d Dokumente.", len(docs), args.max_minutes,
             args.max_docs)
    start = time.monotonic()
    processed = failed = 0
    consecutive_errors = 0

    for doc in docs:
        if processed + failed >= args.max_docs:
            log.info("Dokument-Budget (%d) erreicht - sauber beendet.",
                     args.max_docs)
            break
        if (time.monotonic() - start) >= args.max_minutes * 60:
            log.info("Zeitbudget (%d Min) erreicht - sauber beendet.",
                     args.max_minutes)
            break
        doc_id = doc["id"]
        try:
            process_document(client, analyzer, doc_id, args, tag_ids,
                             document_types)
            processed += 1
            consecutive_errors = 0
        except KeyboardInterrupt:
            log.warning("Abbruch durch Benutzer - Dokument %s geht zurück in "
                        "die Queue.", doc_id)
            best_effort_requeue(client, doc_id, tag_ids)
            return EXIT_OK
        except Exception as exc:
            failed += 1
            consecutive_errors += 1
            log.error("Dokument %s fehlgeschlagen (%s): %s",
                      doc_id, type(exc).__name__, exc)
            best_effort_error(client, doc_id, tag_ids)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("%d Dokumente in Folge fehlgeschlagen - breche "
                          "Lauf ab (Taskplaner ruft später wieder).",
                          consecutive_errors)
                return EXIT_ERROR

    log.info("Lauf beendet: %d verarbeitet, %d fehlerhaft, %.1f Minuten.",
             processed, failed, (time.monotonic() - start) / 60)
    return EXIT_OK


def process_document(client, analyzer, doc_id, args, tag_ids, document_types):
    """Ein Dokument komplett abarbeiten; wirft bei Fehler (Aufrufer markiert)."""
    doc = client.get_document(doc_id)
    client.update_tags(doc_id, add_ids=[tag_ids["processing"]])
    log.info("Dokument %s (%s): lade Original ...", doc_id, doc.get("title"))

    content, content_type = client.download_document(doc_id)
    images = analyzer.pages_to_images(content, content_type,
                                      args.poppler_path)
    if not images:
        raise AnalyzerError("Keine Seiten aus dem Dokument extrahierbar.")
    log.debug("Dokument %s: %d Seite(n) zur Analyse.", doc_id, len(images))

    result = analyzer.analyze(images)
    log.info("Dokument %s: Typ='%s', Korrespondent='%s', Titel='%s', "
             "Datum=%s, Tags=%s", doc_id, result["document_type"],
             result["correspondent"], result["title"], result["date"],
             result["tags"])

    corr_id = resolve_entity(client, result["correspondent"], "correspondent",
                             args.create_entities)
    type_id = resolve_entity(client, result["document_type"],
                             "document_type", args.create_entities)

    doc = client.get_document(doc_id)
    patch = build_patch(doc, result, corr_id, type_id, document_types,
                        client, args.overwrite, args.create_entities)
    if patch:
        log.debug("PATCH Dokument %s: %s", doc_id, patch)
        client.patch_document(doc_id, patch)

    apply_summary(client, doc_id, result["summary"], args)

    # Tag-Übergang erst nach erfolgreichem Rück-Schreiben.
    client.update_tags(doc_id, add_ids=[tag_ids["done"]],
                       remove_ids=[tag_ids["pending"],
                                   tag_ids["processing"]])
    log.info("Dokument %s: abgeschlossen (%s -> %s).", doc_id, TAG_PENDING,
             TAG_DONE)


def build_patch(doc, result, corr_id, type_id, document_types, client,
                overwrite, create_entities):
    """PATCH-Payload nach Pullback-Regeln: nichts vom Menschen Gepflegtes
    überschreiben, außer --overwrite ist gesetzt."""
    patch = {}

    if corr_id is not None and (overwrite or doc.get("correspondent") is None):
        patch["correspondent"] = corr_id

    current_type = doc.get("document_type")
    type_is_default = (
        current_type is None
        or document_types.get(current_type, "").strip().lower() == "sonstiges"
    )
    if type_id is not None and (overwrite or type_is_default):
        patch["document_type"] = type_id

    if result["title"] and (overwrite or title_is_default(doc)):
        patch["title"] = result["title"]

    if result["date"] and (overwrite or not doc.get("created")):
        patch["created"] = result["date"]

    new_tag_ids = merge_keyword_tags(client, result["tags"], create_entities)
    if new_tag_ids:
        patch["tags"] = sorted(set(doc.get("tags") or []) | new_tag_ids)

    return patch


def title_is_default(doc):
    """True, wenn der Titel noch vom Dateinamen kommt bzw. leer ist."""
    title = (doc.get("title") or "").strip()
    if not title:
        return True
    stem = Path(doc.get("original_file_name") or "").stem
    return bool(stem) and title == stem


def merge_keyword_tags(client, names, create_entities):
    ids = set()
    for name in names:
        tag_id = client.find_tag(name)
        if tag_id is None and create_entities:
            tag_id = client.ensure_tag(name)
        if tag_id is not None:
            ids.add(tag_id)
    return ids


def resolve_entity(client, name, kind, create_entities):
    if not name:
        return None
    if kind == "correspondent":
        entity_id = client.find_correspondent(name)
        if entity_id is None and create_entities:
            entity_id = client.create_correspondent(name)
        return entity_id
    if kind == "document_type":
        entity_id = client.find_document_type(name)
        if entity_id is None and create_entities:
            entity_id = client.create_document_type(name)
        return entity_id
    raise ValueError(f"Unbekannte Entitätsart: {kind}")


def apply_summary(client, doc_id, summary, args):
    """Zusammenfassung ablegen: Custom Field 'Zusammenfassung' oder Note."""
    if not summary:
        return
    if args.summary == "note":
        try:
            client.add_note(doc_id, summary)
            return
        except PaperlessAPIError as exc:
            log.error("Dokument %s: Notiz konnte nicht angelegt werden: %s",
                      doc_id, exc)
            raise
    try:
        field_id = client.find_custom_field(SUMMARY_FIELD_NAME)
        if field_id is None:
            field_id = client.create_custom_field(SUMMARY_FIELD_NAME)
        doc = client.get_document(doc_id)
        client.set_custom_field(doc, field_id, summary)
    except PaperlessAPIError as exc:
        log.warning("Dokument %s: Custom Field fehlgeschlagen (%s) - "
                    "Fallback: Notiz.", doc_id, exc)
        try:
            client.add_note(doc_id, summary)
        except PaperlessAPIError as exc_note:
            log.error("Dokument %s: Notiz-Fallback fehlgeschlagen: %s",
                      doc_id, exc_note)
            raise


def recover_stale_processing(client, recovery_minutes, tag_ids):
    """Crash-Recovery: hängende 'llm_processing'-Dokumente zurück in die Queue."""
    try:
        stale = client.list_documents(TAG_PROCESSING)
    except PaperlessAPIError as exc:
        log.warning("Crash-Recovery übersprungen (%s).", exc)
        return
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=recovery_minutes)
    for doc in stale:
        modified = parse_iso_utc(doc.get("modified"))
        if modified is None or modified <= cutoff:
            client.update_tags(doc["id"], add_ids=[tag_ids["pending"]],
                               remove_ids=[tag_ids["processing"]])
            log.info("Crash-Recovery: Dokument %s von '%s' zurück auf '%s'.",
                     doc["id"], TAG_PROCESSING, TAG_PENDING)
        else:
            log.info("Dokument %s ist erst seit Kurzem in Bearbeitung - "
                     "wird ignoriert.", doc["id"])


def best_effort_requeue(client, doc_id, tag_ids):
    """Nach Ctrl+C: 'llm_processing' entfernen, 'llm_pending' sicherstellen."""
    try:
        client.update_tags(doc_id, add_ids=[tag_ids["pending"]],
                           remove_ids=[tag_ids["processing"],
                                       tag_ids["error"]])
    except Exception as exc:
        log.error("Dokument %s konnte nicht zurückgesetzt werden: %s",
                  doc_id, exc)


def best_effort_error(client, doc_id, tag_ids):
    """Nach Fehler: 'llm_pending'/'llm_processing' entfernen, 'llm_error' setzen."""
    try:
        client.update_tags(doc_id, add_ids=[tag_ids["error"]],
                           remove_ids=[tag_ids["pending"],
                                       tag_ids["processing"]])
    except Exception as exc:
        log.error("Dokument %s konnte nicht als '%s' markiert werden: %s",
                  doc_id, TAG_ERROR, exc)


if __name__ == "__main__":
    raise SystemExit(main())
