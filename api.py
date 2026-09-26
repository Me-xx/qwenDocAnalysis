"""REST-Client für die Paperless-ngx API: Token-Auth, Retry mit Backoff, Timeout."""

import logging
import time

import requests

log = logging.getLogger(__name__)


class PaperlessAPIError(Exception):
    """API- oder Netzwerkfehler, der nach allen Retry-Versuchen bestehen bleibt."""


class PaperlessClient:
    """Dünner Wrapper um die Paperless-ngx REST-API.

    Alle Aufrufe nutzen Token-Auth, ein Timeout und bis zu 3 Versuche mit
    Backoff bei 5xx- und Netzwerkfehlern. 4xx-Antworten werden nicht
    wiederholt (z. B. 401 = falsches Token, 404 = Dokument weg).
    """

    def __init__(self, base_url, token, timeout=30.0, retries=3, backoff=2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------ Low level

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs)
                if response.status_code >= 500:
                    raise requests.RequestException(
                        f"Serverfehler HTTP {response.status_code}: "
                        f"{response.text[:200]}")
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff * attempt
                    log.warning(
                        "%s %s fehlgeschlagen (Versuch %d/%d): %s - warte %.0f s",
                        method, path, attempt, self.retries, exc, delay)
                    time.sleep(delay)
        raise PaperlessAPIError(
            f"{method} {url}: nach {self.retries} Versuchen fehlgeschlagen "
            f"({last_error})")

    def _json(self, method, path, ok=(200, 201), **kwargs):
        response = self._request(method, path, **kwargs)
        if response.status_code not in ok:
            raise PaperlessAPIError(
                f"{method} {path}: HTTP {response.status_code}: "
                f"{response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    def _paginate(self, path, params=None):
        params = dict(params or {})
        params.setdefault("page_size", 100)
        results = []
        page = 1
        while True:
            params["page"] = page
            data = self._json("GET", path, params=params)
            results.extend(data.get("results", []))
            if not data.get("next"):
                return results
            page += 1

    # ------------------------------------------------------------ Dokumente

    def list_documents(self, tag_name):
        """Alle Dokumente mit dem gegebenen Tag (Case-insensitive)."""
        return self._paginate("/api/documents/", params={
            "tags__name__iexact": tag_name,
            "truncate_content": "false",
        })

    def get_document(self, doc_id):
        return self._json("GET", f"/api/documents/{doc_id}/")

    def patch_document(self, doc_id, data):
        return self._json("PATCH", f"/api/documents/{doc_id}/", json=data,
                          ok=(200,))

    def download_document(self, doc_id):
        """Original-Datei (PDF/Bild) als Bytes, samt Content-Type."""
        response = self._request("GET", f"/api/documents/{doc_id}/download/")
        if response.status_code != 200:
            raise PaperlessAPIError(
                f"Download {doc_id}: HTTP {response.status_code}")
        return response.content, response.headers.get("Content-Type", "")

    def add_note(self, doc_id, text):
        self._json("POST", f"/api/documents/{doc_id}/notes/",
                   json={"note": text}, ok=(200, 201, 204))

    def update_tags(self, doc_id, add_ids=(), remove_ids=()):
        """Tags des Dokuments anpassen (read-modify-write auf ID-Ebene)."""
        doc = self.get_document(doc_id)
        current = set(doc.get("tags") or [])
        updated = (current - set(remove_ids)) | set(add_ids)
        if updated == current:
            return doc
        return self.patch_document(doc_id, {"tags": sorted(updated)})

    def set_custom_field(self, doc, field_id, value):
        """Custom-Field-Wert setzen; bestehende Einträge bleiben erhalten."""
        entries = [dict(e) for e in (doc.get("custom_fields") or [])]
        for entry in entries:
            if entry.get("custom_field") == field_id:
                entry["value"] = value
                break
        else:
            entries.append({"custom_field": field_id, "value": value})
        return self.patch_document(doc["id"], {"custom_fields": entries})

    # ------------------------------------------------------------ Tags

    def list_tags(self):
        return self._paginate("/api/tags/")

    def find_tag(self, name):
        for tag in self.list_tags():
            if tag.get("name", "").lower() == str(name).lower():
                return tag["id"]
        return None

    def ensure_tag(self, name):
        tag_id = self.find_tag(name)
        if tag_id is not None:
            return tag_id
        created = self._json("POST", "/api/tags/", json={"name": name})
        log.info("Tag '%s' angelegt (ID %s).", name, created["id"])
        return created["id"]

    # ------------------------------------------------------------ Entitäten

    def list_correspondents(self):
        return self._paginate("/api/correspondents/")

    def find_correspondent(self, name):
        for corr in self.list_correspondents():
            if corr.get("name", "").lower() == str(name).lower():
                return corr["id"]
        return None

    def create_correspondent(self, name):
        created = self._json("POST", "/api/correspondents/",
                             json={"name": name})
        log.info("Korrespondent '%s' angelegt (ID %s).", name, created["id"])
        return created["id"]

    def list_document_types(self):
        return self._paginate("/api/document_types/")

    def find_document_type(self, name):
        for dtype in self.list_document_types():
            if dtype.get("name", "").lower() == str(name).lower():
                return dtype["id"]
        return None

    def create_document_type(self, name):
        created = self._json("POST", "/api/document_types/",
                             json={"name": name})
        log.info("Dokumenttyp '%s' angelegt (ID %s).", name, created["id"])
        return created["id"]

    # ------------------------------------------------------------ Custom Fields

    def list_custom_fields(self):
        return self._paginate("/api/custom_fields/")

    def find_custom_field(self, name):
        for field in self.list_custom_fields():
            if field.get("name", "").lower() == str(name).lower():
                return field["id"]
        return None

    def create_custom_field(self, name, data_type="string"):
        created = self._json("POST", "/api/custom_fields/",
                             json={"name": name, "data_type": data_type})
        log.info("Custom Field '%s' angelegt (ID %s).", name, created["id"])
        return created["id"]
