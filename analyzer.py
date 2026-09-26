"""Qwen2-VL Modell-Wrapper: Laden (INT4/NF4), Seitenextraktion, JSON-Analyse."""

import io
import json
import logging
import re
from datetime import datetime

from PIL import Image

log = logging.getLogger(__name__)

# Feste Whitelist für document_type; alles Unbekannte wird "Sonstiges".
DOCUMENT_TYPES = [
    "Rechnung",
    "Behördenschreiben",
    "Bedienungsanleitung",
    "Lieferschein",
    "Vertrag",
    "Sonstiges",
]

PROMPT = """\
Du bist ein Assistent, der gescannte Dokumente klassifiziert. Analysiere das \
abgebildete Dokument (eine oder mehrere Seiten, zusammengehörig).

Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt, ohne Markdown, ohne \
Einleitung und ohne Erklärung.

Schema:
{
  "correspondent": "Absender des Dokuments (Firma, Behörde, Person) oder null",
  "document_type": "Rechnung | Behördenschreiben | Bedienungsanleitung | Lieferschein | Vertrag | Sonstiges",
  "title": "kurzer aussagekräftiger Titel",
  "date": "Datum des Dokuments im Format YYYY-MM-DD oder null",
  "summary": "Zusammenfassung des Inhalts in 1-2 Sätzen",
  "tags": ["0 bis 3 kurze Schlagworte"]
}

Regeln:
- correspondent ist der ABSENDER des Dokuments (Briefkopf, Absenderangabe), \
niemals der eigene Name und niemals der Empfänger.
- document_type NUR aus der genannten Liste wählen; passt nichts, wähle \
"Sonstiges".
- date ist das auf dem Dokument gedruckte Datum (z.B. Rechnungs- oder \
Briefdatum), nicht das heutige Datum.
- Antworte nur mit dem JSON-Objekt."""


class AnalyzerError(Exception):
    """Fehler bei der Modell-Inferenz oder der JSON-Auswertung."""


class QwenAnalyzer:
    """Lädt Qwen2-VL lokal in INT4/NF4 (bitsandbytes) und analysiert Dokumente.

    Bewusst NICHT verwendet: torch.compile (instabil bei 4-bit-Modellen mit
    dynamischen Bildgrößen).
    """

    def __init__(self, model_path, max_pages=3, max_side=1280,
                 max_new_tokens=512,
                 min_pixels=256 * 28 * 28,
                 max_pixels=512 * 28 * 28):
        self.model_path = str(model_path)
        self.max_pages = max_pages
        self.max_side = max_side
        self.max_new_tokens = max_new_tokens
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels
        self.model = None
        self.processor = None
        self.device = None

    # --------------------------------------------------------- Modell

    def load(self):
        import torch
        from transformers import (AutoModelForVision2Seq, AutoProcessor,
                                  BitsAndBytesConfig)

        if not torch.cuda.is_available():
            raise AnalyzerError("Keine CUDA-fähige GPU verfügbar.")

        log.info("Lade Qwen2-VL aus %s (INT4/NF4 via bitsandbytes) ...",
                 self.model_path)
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
        )
        self.processor = AutoProcessor.from_pretrained(
            self.model_path,
            trust_remote_code=True,
            min_pixels=self.min_pixels,
            max_pixels=self.max_pixels,
        )
        self.model = AutoModelForVision2Seq.from_pretrained(
            self.model_path,
            quantization_config=bnb_config,
            device_map="auto",
            low_cpu_mem_usage=True,
            trust_remote_code=True,
        )
        self.model.eval()
        self.device = next(self.model.parameters()).device
        log.info("Modell bereit (device=%s).", self.device)

    # --------------------------------------------------------- Seiten

    def pages_to_images(self, data, content_type="", poppler_path=None):
        """Bytes eines Dokuments in RGB-Bilder umwandeln (max. max_pages).

        PDFs werden über pdf2image/poppler gerastert, Bilder direkt geöffnet.
        Die längste Bildkante wird auf max_side begrenzt (VRAM-Schutz).
        """
        if data[:5] == b"%PDF-" or "pdf" in (content_type or "").lower():
            pages = self._pdf_pages(data, poppler_path)
        else:
            pages = [Image.open(io.BytesIO(data))]

        images = []
        for img in pages:
            img = img.convert("RGB")
            if max(img.size) > self.max_side:
                img.thumbnail((self.max_side, self.max_side), Image.LANCZOS)
            images.append(img)
        return images

    def _pdf_pages(self, data, poppler_path):
        from pdf2image import convert_from_bytes, pdfinfo_from_bytes

        n_pages = self.max_pages
        try:
            info = pdfinfo_from_bytes(data, poppler_path=poppler_path)
            n_pages = int(info.get("Pages") or self.max_pages)
        except Exception as exc:  # pdfinfo optional, Extraktion klappt trotzdem
            log.warning("pdfinfo fehlgeschlagen (%s) - extrahiere bis zu %d "
                        "Seiten.", exc, self.max_pages)
        last = max(1, min(n_pages, self.max_pages))
        return convert_from_bytes(data, dpi=200, first_page=1, last_page=last,
                                  poppler_path=poppler_path)

    # --------------------------------------------------------- Inferenz

    def analyze(self, images):
        """Seiten analysieren; liefert das validierte Ergebnis-Dict."""
        raw = self._generate(images)
        result = self.parse_result(raw)
        log.debug("Analysiertes Ergebnis: %s", result)
        return result

    def _generate(self, images):
        import torch

        content = [{"type": "image", "image": img} for img in images]
        content.append({"type": "text", "text": PROMPT})
        messages = [{"role": "user", "content": content}]

        prompt_text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=[prompt_text], images=list(images),
                                return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}

        with torch.inference_mode():
            output = self.model.generate(
                **inputs, max_new_tokens=self.max_new_tokens, do_sample=False)

        # Nur die neu generierten Tokens dekodieren (Prompt nicht mitausgeben).
        input_len = inputs["input_ids"].shape[1]
        generated = output[:, input_len:]
        raw = self.processor.batch_decode(generated,
                                          skip_special_tokens=True)[0]
        log.debug("Roh-Ausgabe Qwen: %r", raw)
        return raw

    # --------------------------------------------------------- Validierung

    def parse_result(self, raw):
        data = extract_json_object(raw)
        if not isinstance(data, dict):
            raise AnalyzerError("JSON-Antwort ist kein Objekt.")
        return {
            "correspondent": clean_text(data.get("correspondent"), 128),
            "document_type": match_document_type(data.get("document_type")),
            "title": clean_text(data.get("title"), 128),
            "date": parse_date(data.get("date")),
            "summary": clean_text(data.get("summary"), 2000),
            "tags": clean_tags(data.get("tags")),
        }


# ---------------------------------------------------------------- Helfer

def extract_json_object(text):
    """Ersten JSON-Block aus der Modell-Ausgabe ziehen und reparieren."""
    text = text.strip()
    candidates = []
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    match = re.search(r"\{.*", text, re.DOTALL)
    if match:
        raw = match.group(0).rstrip().rstrip(",;")
        # Abgeschnittene Antworten reparieren: fehlendes '"' und '}' ergänzen.
        candidates.append(raw + '"\n}')

    for raw in candidates:
        parsed = _try_load_json(raw)
        if parsed is not None:
            return parsed
    raise AnalyzerError(
        f"Kein gültiges JSON in der Modell-Ausgabe: {text[:300]!r}")


def _try_load_json(raw):
    raw = raw.replace("\x00", "")
    raw = re.sub(r",\s*([}\]])", r"\1", raw)  # Trailing-Kommas
    for candidate in (raw, raw + '"', raw + '"}'):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def clean_text(value, max_len):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in ("null", "none", "unknown", "unbekannt",
                                    "n/a"):
        return None
    return text[:max_len]


def match_document_type(value):
    """Whitelist-Match; alles Unbekannte/leere wird 'Sonstiges'."""
    if value is None:
        return "Sonstiges"
    text = str(value).strip().lower()
    if not text:
        return "Sonstiges"
    for name in DOCUMENT_TYPES:
        if name.lower() == text:
            return name
    for name in DOCUMENT_TYPES:
        if name.lower() in text:
            return name
    return "Sonstiges"


def parse_date(value):
    """Datum in ISO (YYYY-MM-DD) normalisieren; sonst None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "unknown"):
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    return None


def clean_tags(value):
    if not isinstance(value, list):
        return []
    tags = []
    for item in value:
        text = clean_text(item, 64)
        if text:
            tags.append(text)
    return tags[:5]
