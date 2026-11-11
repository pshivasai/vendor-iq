import os
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="vendor-iq-tests-")
os.environ["VERIFICATION_MODE"] = "demo"

from app.database import Base, get_db
from app.main import app
from app.services.review import tax_format

TEST_ENGINE = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=TEST_ENGINE)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_test_database(monkeypatch):
    # Keep tests offline and deterministic: no real Gemini calls, no real SMTP.
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "smtp_user", None)
    Base.metadata.drop_all(bind=TEST_ENGINE)
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield


def fake_ai(score, status):
    def _score(vendor, documents=None):
        level = "LOW" if score <= 30 else ("HIGH" if score >= 70 else "MEDIUM")
        return {"risk_score": score, "risk_level": level, "reason": "stubbed", "detail_notes": "", "final_status": status}
    return _score


BASE = {
    "legal_name": "Aarav Industrial Supplies Private Limited",
    "country_code": "IN",
    "tax_id_type": "GSTIN",
    "tax_id": "29ABCDE1234F1Z5",
    "registration_id": "U27100KA2020PTC123456",
    "registered_address": "Plot 12, Peenya Industrial Area, Bengaluru, Karnataka, India",
    "contact_name": "Riya Sharma",
    "contact_email": "vendor@example.com",
    "supplier_category": "Industrial supplies",
    "business_justification": "Supplier for approved maintenance and plant operations requirements.",
    "bank_account_holder": "Aarav Industrial Supplies Private Limited",
    "bank_country": "IN",
    "bank_code": "HDFC0001234",
    "bank_reference": "DEMO-BANK-REF-001",
}


def create_vendor(payload=None):
    response = client.post("/api/submissions", json=payload or BASE)
    assert response.status_code == 201, response.text
    return response.json()


def upload_text(vendor_id, filename, doc_type, text):
    response = client.post(
        f"/api/submissions/{vendor_id}/documents",
        data={"document_type": doc_type},
        files={"file": (filename, text.encode(), "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_happy_path_docs(vendor_id):
    upload_text(vendor_id, "registration.txt", "registration_certificate",
                "LEGAL NAME: Aarav Industrial Supplies Private Limited\nREGISTRATION: U27100KA2020PTC123456")
    upload_text(vendor_id, "tax.txt", "tax_certificate", "GSTIN: 29ABCDE1234F1Z5\nLEGAL NAME: Aarav Industrial Supplies Private Limited")
    upload_text(vendor_id, "bank.txt", "bank_proof", "BANK ACCOUNT HOLDER: Aarav Industrial Supplies Private Limited")


def test_happy_path_approved_in_demo_mode():
    vendor = create_vendor()
    upload_happy_path_docs(vendor["id"])
    result = client.post(f"/api/submissions/{vendor['id']}/evaluate")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "APPROVED"
    assert {r["stage"] for r in body["reviews"]} == {
        "DOCUMENT_VERIFICATION", "IDENTITY_TAX_VALIDATION", "FINANCIAL_COMPLIANCE",
        "BUSINESS_PROCUREMENT", "AI_RISK_ASSESSMENT",
    }
    assert body["communications"][-1]["subject"].startswith("✅") or "approved" in body["communications"][-1]["subject"].lower()


def test_edge_identity_conflict_requests_clarification():
    vendor = create_vendor()
    upload_text(vendor["id"], "registration.txt", "registration_certificate",
                "LEGAL NAME: Different Trading Company Limited")
    upload_happy_path_docs(vendor["id"])
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    assert body["status"] == "PENDING"
    assert any("differs from the submitted legal name" in f for r in body["reviews"] for f in r["findings"])
    assert body["communications"]
    assert any("upload a corrected document" in a.lower() for r in body["reviews"] for a in r["required_actions"])


def test_edge_duplicate_vendor_is_flagged_not_merged():
    first = create_vendor()
    upload_happy_path_docs(first["id"])
    first_result = client.post(f"/api/submissions/{first['id']}/evaluate").json()
    assert first_result["status"] == "APPROVED"
    second_payload = dict(BASE)
    second_payload["legal_name"] = "Aarav Industrial Supplies Pvt Ltd"
    second_payload["contact_email"] = "accounts@example.org"
    second = create_vendor(second_payload)
    body = client.post(f"/api/submissions/{second['id']}/evaluate").json()
    assert body["status"] == "PENDING"
    procurement = next(r for r in body["reviews"] if r["stage"] == "BUSINESS_PROCUREMENT")
    assert any("Potential duplicate supplier" in f for f in procurement["findings"])


def test_edge_bank_holder_mismatch_holds_finance_approval():
    payload = dict(BASE)
    payload["bank_account_holder"] = "XYZ Trading Enterprises"
    vendor = create_vendor(payload)
    upload_happy_path_docs(vendor["id"])
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    assert body["status"] == "PENDING"
    finance = next(r for r in body["reviews"] if r["stage"] == "FINANCIAL_COMPLIANCE")
    assert finance["status"] == "NEEDS_INFORMATION"
    assert any("does not sufficiently match" in f for f in finance["findings"])


def test_edge_country_specific_tax_identifier_rules():
    # UK VAT uses its own rule; it must not be checked against the Indian GSTIN pattern.
    assert tax_format("GB", "VAT", "GB123456789")[0] is True
    assert tax_format("GB", "VAT", "29ABCDE1234F1Z5")[0] is False
    payload = dict(BASE)
    payload.update({
        "legal_name": "Northstar Components Limited",
        "country_code": "GB", "tax_id_type": "VAT", "tax_id": "GB123456789",
        "bank_country": "GB", "bank_code": "123456", "bank_account_holder": "Northstar Components Limited",
        "bank_reference": "DEMO-UK-BANK-001", "contact_email": "ops@northstar.example",
    })
    vendor = create_vendor(payload)
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    compliance = next(r for r in body["reviews"] if r["stage"] == "IDENTITY_TAX_VALIDATION")
    assert not any("does not match the expected format" in f for f in compliance["findings"])


def test_dashboard_and_audit_endpoints():
    vendor = create_vendor()
    client.post(f"/api/submissions/{vendor['id']}/evaluate")
    stats = client.get("/api/dashboard/stats")
    assert stats.status_code == 200
    assert stats.json()["total"] >= 1
    audit = client.get(f"/api/submissions/{vendor['id']}/audit")
    assert audit.status_code == 200
    assert any(row["action"] == "GEMINI_EVALUATION_COMPLETED" for row in audit.json())


def test_confirmed_registry_failure_returns_rejected(monkeypatch):
    from app.services import review as review_service
    monkeypatch.setattr(review_service.settings, "demo_blocked_tax_ids", BASE["tax_id"])
    vendor = create_vendor()
    upload_happy_path_docs(vendor["id"])
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    assert body["status"] == "REJECTED"
    assert body["communications"]
    compliance = next(r for r in body["reviews"] if r["stage"] == "IDENTITY_TAX_VALIDATION")
    assert compliance["status"] == "FAIL"


def test_ai_cannot_approve_when_rule_checks_need_information(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "score_vendor_risk", fake_ai(5, "APPROVED"))
    payload = dict(BASE)
    payload["bank_account_holder"] = "XYZ Trading Enterprises"
    vendor = create_vendor(payload)
    upload_happy_path_docs(vendor["id"])
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    assert body["status"] == "PENDING"


def test_ai_decides_when_rule_checks_pass(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "score_vendor_risk", fake_ai(85, "REJECTED"))
    vendor = create_vendor()
    upload_happy_path_docs(vendor["id"])
    body = client.post(f"/api/submissions/{vendor['id']}/evaluate").json()
    assert body["status"] == "REJECTED"
    assert body["ai_risk_score"] == 85
