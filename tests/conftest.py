"""Pytest fixtures for Document Intelligence integration tests.

Loads sample files from $DI_DATASET_DIR, uploads them, waits for final processing,
and provides shared test fixtures.
"""
import os
import time
import pytest
import requests

BASE = "https://assignment02.app.n8n.cloud/webhook"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
FINAL = {"completed", "needs_review", "failed"}


def get_key():
    key = os.environ.get("DI_API_KEY")
    if not key:
        pytest.fail("Environment variable DI_API_KEY is required.")
    return key


def headers(key=None):
    return {"apikey": key or get_key()}


def get(path, **params):
    """Helper for GET requests."""
    return requests.get(f"{BASE}{path}", headers=headers(), params=params, timeout=60)


def upload(filename, content, key=None):
    """Uploads a file with explicit tuple structure so n8n receives proper multipart boundaries."""
    url = f"{BASE}/invoice-upload"
    files = {"file": (filename, content, "application/octet-stream")}
    return requests.post(url, headers=headers(key), files=files, timeout=60)


def wait_final(doc_id, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/api-document", headers=headers(), params={"id": doc_id}, timeout=60)
        if r.status_code == 200:
            doc = r.json()
            if doc.get("status") in FINAL:
                return doc
        time.sleep(3)
    pytest.fail(f"Document {doc_id} did not reach final state within {timeout}s")


def fetch_result(doc_id):
    r = requests.get(f"{BASE}/api-result", headers=headers(), params={"id": doc_id}, timeout=60)
    if r.status_code != 200:
        return {}
    return r.json()


@pytest.fixture(scope="session")
def dataset_dir():
    d = os.environ.get("DI_DATASET_DIR")
    if not d or not os.path.isdir(d):
        pytest.fail("Environment variable DI_DATASET_DIR must point to a valid dataset directory.")
    return d


@pytest.fixture(scope="session")
def upload_sample(dataset_dir):
    def _upload(filename):
        path = os.path.join(dataset_dir, filename)
        if not os.path.exists(path):
            pytest.fail(f"Sample file not found: {path}")
        with open(path, "rb") as f:
            content = f.read()
        r = upload(filename, content)
        assert r.status_code == 202, f"Upload failed for {filename}: {r.text}"
        data = r.json()
        doc_id = data.get("document_id")
        assert doc_id, f"No document_id returned for {filename}: {data}"
        
        doc_status = wait_final(doc_id)
        result = fetch_result(doc_id)
        return {
            "id": doc_id,
            "filename": filename,
            "status": doc_status,
            "result": result
        }
    return _upload


@pytest.fixture(scope="session")
def digital(upload_sample):
    return upload_sample("digital_01.pdf")


@pytest.fixture(scope="session")
def csv_doc(upload_sample):
    return upload_sample("table_01.csv")


@pytest.fixture(scope="session")
def xlsx_doc(upload_sample):
    return upload_sample("sheet_01.xlsx")


@pytest.fixture(scope="session")
def corrupt_doc(upload_sample):
    return upload_sample("corrupt_truncated.pdf")