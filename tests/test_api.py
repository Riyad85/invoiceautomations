"""Integration tests for the Document Intelligence API (live n8n workflows).

Groups: security, upload validation, pipeline per file type, failure handling,
read API, manual correction, reprocessing.
"""
import requests

from conftest import (BASE, UNKNOWN_ID, FINAL, get, headers, upload, wait_final)


def inv_of(doc):
    invs = doc["result"].get("invoices") or []
    return invs[0] if invs else None


# ---------------------------------------------------------------- security
class TestSecurity:
    def test_upload_without_key_rejected(self):
        r = requests.post(f"{BASE}/invoice-upload", files={"file": ("a.csv", b"a,b\n1,2")}, timeout=60)
        assert r.status_code == 401

    def test_upload_wrong_key_rejected(self):
        assert upload("a.csv", b"a,b\n1,2", key="wrong-key").status_code == 401

    def test_read_endpoints_need_key(self):
        for path in ("/api-documents", "/api-document?id=" + UNKNOWN_ID, "/api-result?id=" + UNKNOWN_ID):
            r = requests.get(BASE + path, timeout=60)
            assert r.status_code in (401, 403), path

    def test_wrong_key_on_read_rejected(self):
        r = requests.get(f"{BASE}/api-documents", headers=headers("wrong-key"), timeout=60)
        assert r.status_code in (401, 403)


# ----------------------------------------------------------- upload checks
class TestUploadValidation:
    def test_unsupported_extension_rejected(self):
        r = upload("notes.txt", b"hello")
        assert r.status_code == 400 and "Unsupported" in r.json()["error"]

    def test_no_file_rejected(self):
        r = requests.post(f"{BASE}/invoice-upload", headers=headers(), data={"x": "1"}, timeout=60)
        assert r.status_code == 400

    def test_upload_is_asynchronous(self, digital):
        # The upload call returned 202 "queued" immediately; processing continued in the background.
        hist = [h["step"] for h in digital["status"]["processing_history"]]
        assert hist[0] == "upload"


# ------------------------------------------------------- pipeline per type
class TestPipeline:
    def test_digital_pdf_finishes(self, digital):
        assert digital["status"]["status"] in {"completed", "needs_review"}

    def test_digital_pdf_extracts_core_fields(self, digital):
        inv = inv_of(digital)
        assert inv is not None
        assert inv["invoice_number"] and inv["supplier_name"] and inv["total"] is not None
        assert len(inv["line_items"]) >= 1

    def test_digital_pdf_history_has_every_stage(self, digital):
        steps = {h["step"] for h in digital["status"]["processing_history"]}
        assert {"upload", "text_extraction", "llm_extraction", "validation"} <= steps

    def test_csv_parsed(self, csv_doc):
        assert csv_doc["status"]["status"] in {"completed", "needs_review"}
        assert inv_of(csv_doc) is not None

    def test_xlsx_parsed(self, xlsx_doc):
        assert xlsx_doc["status"]["status"] in {"completed", "needs_review"}
        assert inv_of(xlsx_doc) is not None


# ------------------------------------------------------------ failure case
class TestFailures:
    def test_corrupt_file_fails_cleanly(self, corrupt_doc):
        assert corrupt_doc["status"]["status"] == "failed"
        assert corrupt_doc["status"]["error_message"]

    def test_corrupt_file_has_no_invented_invoice(self, corrupt_doc):
        assert inv_of(corrupt_doc) is None

    def test_failure_is_logged(self, corrupt_doc):
        assert any(h["status"] == "failed" for h in corrupt_doc["status"]["processing_history"])


# ---------------------------------------------------------------- read API
class TestReadAPI:
    def test_list_returns_documents(self, digital):
        r = get("/api-documents", limit=100)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == len(body["data"]) and body["count"] >= 1
        assert digital["id"] in {d["id"] for d in body["data"]}

    def test_filter_by_status(self, corrupt_doc):
        r = get("/api-documents", status="failed", limit=100)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data and all(d["status"] == "failed" for d in data)
        assert corrupt_doc["id"] in {d["id"] for d in data}

    def test_keyword_search_finds_document(self, digital):
        number = inv_of(digital)["invoice_number"]
        r = get("/api-documents", q=number, limit=100)
        assert r.status_code == 200
        assert digital["id"] in {d["id"] for d in r.json()["data"]}

    def test_supplier_filter(self, digital):
        supplier = inv_of(digital)["supplier_name"]
        r = get("/api-documents", supplier=supplier.split()[0], limit=100)
        assert r.status_code == 200
        assert digital["id"] in {d["id"] for d in r.json()["data"]}

    def test_hostile_search_text_does_not_crash(self):
        r = get("/api-documents", q="'; drop table documents; --", supplier="%27%22<script>")
        assert r.status_code == 200

    def test_unknown_document_404(self):
        assert get("/api-document", id=UNKNOWN_ID).status_code == 404
        assert get("/api-result", id=UNKNOWN_ID).status_code == 404

    def test_malformed_id_does_not_crash(self):
        assert get("/api-document", id="not-a-uuid").status_code == 404


# -------------------------------------------------------- manual correction
class TestCorrection:
    def patch(self, body):
        return requests.patch(f"{BASE}/api-correct", headers=headers(), json=body, timeout=60)

    def test_missing_invoice_id(self):
        assert self.patch({"fields": {"total": 1}}).status_code == 400

    def test_no_fields(self, xlsx_doc):
        assert self.patch({"invoice_id": inv_of(xlsx_doc)["id"], "fields": {}}).status_code == 400

    def test_bad_date_rejected(self, xlsx_doc):
        r = self.patch({"invoice_id": inv_of(xlsx_doc)["id"], "fields": {"invoice_date": "04/11/2026"}})
        assert r.status_code == 400

    def test_non_numeric_total_rejected(self, xlsx_doc):
        r = self.patch({"invoice_id": inv_of(xlsx_doc)["id"], "fields": {"total": "abc"}})
        assert r.status_code == 400

    def test_unknown_invoice_404(self):
        assert self.patch({"invoice_id": UNKNOWN_ID, "fields": {"total": 1}}).status_code == 404

    def test_valid_correction_is_saved_and_revalidated(self, xlsx_doc):
        inv = inv_of(xlsx_doc)
        new_name = "Corrected Supplier Ltd"
        r = self.patch({"invoice_id": inv["id"], "fields": {"supplier_name": new_name}})
        assert r.status_code == 200 and "supplier_name" in r.json()["corrected_fields"]
        wait_final(xlsx_doc["id"])                       # validation re-runs in the background
        after = get("/api-result", id=xlsx_doc["id"]).json()["invoices"][0]
        assert after["supplier_name"] == new_name
        assert after["confidence"]["supplier_name"] == 1  # human-confirmed
        steps = [h["step"] for h in get("/api-document", id=xlsx_doc["id"]).json()["processing_history"]]
        assert "correction" in steps and steps.count("validation") >= 2


# -------------------------------------------------------------- reprocess
class TestReprocess:
    def post(self, doc_id):
        return requests.post(f"{BASE}/api-reprocess", headers=headers(), json={"document_id": doc_id}, timeout=60)

    def test_unknown_document_404(self):
        assert self.post(UNKNOWN_ID).status_code == 404

    def test_malformed_id_404(self):
        assert self.post("nope").status_code == 404

    def test_failed_document_can_be_reprocessed(self, corrupt_doc):
        before = corrupt_doc["status"]["attempts"]
        r = self.post(corrupt_doc["id"])
        assert r.status_code == 202 and r.json()["status"] == "queued"
        after = wait_final(corrupt_doc["id"])
        assert after["status"] in FINAL
        assert after["attempts"] > before
