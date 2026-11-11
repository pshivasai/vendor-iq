from __future__ import annotations
from datetime import date, datetime
from difflib import SequenceMatcher
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import VendorSubmission, VendorDocument, ApprovedVendor
from ..config import settings

PASS = "PASS"
NEEDS = "NEEDS_INFORMATION"
FAIL = "FAIL"


def normalize_name(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    tokens = [t for t in value.split() if t not in {"private", "limited", "ltd", "pvt", "llp", "inc", "incorporated", "corp", "corporation", "company", "co"}]
    return " ".join(tokens)


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return max(SequenceMatcher(None, na, nb).ratio(), len(set(na.split()) & set(nb.split())) / max(1, len(set(na.split()) | set(nb.split()))))


def tax_format(country: str, tax_type: str, tax_id: str) -> tuple[bool, str]:
    value = re.sub(r"[\s.-]", "", tax_id.upper())
    patterns = {
        ("IN", "GSTIN"): r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$",
        ("IN", "PAN"): r"^[A-Z]{5}[0-9]{4}[A-Z]$",
        ("US", "EIN"): r"^[0-9]{9}$",
        ("GB", "VAT"): r"^GB[0-9]{9}([0-9]{3})?$",
        ("AU", "ABN"): r"^[0-9]{11}$",
        ("SG", "UEN"): r"^[A-Z0-9]{8,10}$",
    }
    pattern = patterns.get((country.upper(), tax_type.upper()))
    if not pattern:
        return False, f"Tax ID type {tax_type} is not configured for country {country}."
    if re.fullmatch(pattern, value):
        return True, f"{tax_type} format is valid for {country}; this is a format check, not registry verification."
    return False, f"{tax_type} does not match the expected format for {country}."


def _docs_by_type(documents: list[VendorDocument]) -> dict[str, list[VendorDocument]]:
    aliases = {
        "gst_certificate": "tax_certificate", "pan_document": "tax_certificate",
        "pan_card": "tax_certificate", "vat_certificate": "tax_certificate",
        "ein_letter": "tax_certificate", "abn_certificate": "tax_certificate",
        "certificate_of_incorporation": "registration_certificate", "business_registration": "registration_certificate",
        "bank_statement": "bank_proof", "void_cheque": "bank_proof",
    }
    result: dict[str, list[VendorDocument]] = {}
    for doc in documents:
        canonical = aliases.get(doc.document_type, doc.document_type)
        result.setdefault(canonical, []).append(doc)
    return result


def _doc_identity_findings(vendor: VendorSubmission, documents: list[VendorDocument]) -> tuple[list[str], list[str]]:
    findings, actions = [], []
    for doc in documents:
        fields = (doc.extraction_json or {}).get("fields", {})
        extracted_name = fields.get("legal_name") or fields.get("company_name")
        if extracted_name and name_similarity(vendor.legal_name, str(extracted_name)) < 0.72:
            findings.append(f"Name in {doc.original_filename} ('{extracted_name}') differs from the submitted legal name.")
            actions.append(f"Confirm the legal entity name shown on {doc.original_filename} or upload a corrected document.")
        extracted_ids = [fields.get(k) for k in ("tax_id", "gstin", "pan", "vat", "ein", "abn") if fields.get(k)]
        if extracted_ids and not any(_tax_ids_consistent(vendor.tax_id, str(x)) for x in extracted_ids):
            findings.append(f"Tax identifier in {doc.original_filename} does not match the submitted tax ID.")
            actions.append(f"Confirm the tax identifier or upload the correct {doc.document_type.replace('_', ' ')}.")
    return findings, actions


def _tax_ids_consistent(submitted: str, extracted: str) -> bool:
    """Same ID, or an Indian PAN that is embedded in a GSTIN (GSTIN characters 3-12 are the PAN)."""
    a, b = re.sub(r"\W", "", submitted).upper(), re.sub(r"\W", "", extracted).upper()
    if a == b:
        return True
    gstin = r"[0-9]{2}([A-Z]{5}[0-9]{4}[A-Z])[1-9A-Z]Z[0-9A-Z]"
    pan = r"[A-Z]{5}[0-9]{4}[A-Z]"
    for g, p in ((a, b), (b, a)):
        m = re.fullmatch(gstin, g)
        if m and re.fullmatch(pan, p) and m.group(1) == p:
            return True
    return False


def document_verification(docs: list[VendorDocument]) -> dict:
    findings, evidence, actions = [], [], []
    doc_map = _docs_by_type(docs)

    for required_type, label in [
        ("registration_certificate", "company registration certificate"),
        ("tax_certificate", "tax registration certificate"),
    ]:
        if required_type not in doc_map:
            findings.append(f"Required {label} is missing.")
            actions.append(f"Upload the {label}.")
        elif all(not doc.extracted_text.strip() for doc in doc_map[required_type]):
            findings.append(f"The {label} could not be read by the document processor.")
            actions.append(f"Upload a clearer, readable copy of the {label}.")

    if "registration_certificate" in doc_map:
        readable_registration = any(
            (doc.extraction_json or {}).get("fields", {}).get("legal_name")
            or (doc.extraction_json or {}).get("fields", {}).get("registration_id")
            for doc in doc_map["registration_certificate"]
        )
        if not readable_registration and any(doc.extracted_text.strip() for doc in doc_map["registration_certificate"]):
            findings.append("No company name or registration identifier could be extracted from the registration certificate.")
            actions.append("Confirm the company details and upload a registration certificate where the legal name and registration number are legible.")

    if "tax_certificate" in doc_map:
        tax_keys = {"tax_id", "gstin", "pan", "vat", "ein", "abn", "uen"}
        extracted_tax = any(
            tax_keys.intersection((doc.extraction_json or {}).get("fields", {}).keys())
            for doc in doc_map["tax_certificate"]
        )
        if not extracted_tax and any(doc.extracted_text.strip() for doc in doc_map["tax_certificate"]):
            findings.append("No tax identifier could be extracted from the tax certificate.")
            actions.append("Upload a readable tax certificate showing the submitted tax identifier.")

    if "bank_proof" not in doc_map:
        findings.append("Bank proof document has not been supplied.")
        actions.append("Upload a bank proof document (for example, a bank letter or void cheque) showing the account holder and bank details.")
    elif all(not doc.extracted_text.strip() for doc in doc_map["bank_proof"]):
        findings.append("Bank proof could not be read by the document processor.")
        actions.append("Upload a clear, readable bank letter or statement; do not include an unredacted full account number.")

    status = NEEDS if actions else PASS
    if status == PASS:
        findings.append("All required documents (registration certificate, tax certificate, bank proof) uploaded and readable.")
        evidence.append("Document completeness and text extraction checks passed.")
        summary = "All required documents uploaded and verified readable."
    else:
        summary = f"{len(actions)} document issue(s) require attention."

    return {
        "stage": "DOCUMENT_VERIFICATION",
        "stage_order": 1,
        "status": status,
        "findings": findings,
        "evidence": evidence,
        "required_actions": actions,
        "summary": summary,
    }


def identity_tax_validation(vendor: VendorSubmission, docs: list[VendorDocument]) -> dict:
    findings, evidence, actions = [], [], []
    valid, message = tax_format(vendor.country_code, vendor.tax_id_type, vendor.tax_id)
    if valid:
        evidence.append(message)
    else:
        findings.append(message)
        actions.append(f"Correct the {vendor.tax_id_type} or select the appropriate tax identifier type for {vendor.country_code}.")

    # This server-side demo fixture emulates a trusted provider's confirmed adverse result.
    # It is never populated from vendor-submitted fields and is disabled by default.
    blocked_ids = {item.strip().upper() for item in settings.demo_blocked_tax_ids.split(",") if item.strip()}
    hard_failure = settings.verification_mode == "demo" and vendor.tax_id.upper() in blocked_ids
    if hard_failure:
        findings.append("Trusted demo registry fixture returned a confirmed inactive/blocked tax registration.")
        actions.append("Contact the compliance team and provide evidence of an active, valid tax registration.")

    identity_findings, identity_actions = _doc_identity_findings(vendor, docs)
    if identity_findings:
        findings.extend(identity_findings)
        actions.extend(identity_actions)
        evidence.append("Document-to-submission cross-reference found conflicting identity details.")

    expired = []
    for doc in docs:
        expiry = (doc.extraction_json or {}).get("fields", {}).get("expiry_date")
        if expiry:
            parsed = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(str(expiry), fmt).date()
                    break
                except ValueError:
                    pass
            if parsed and parsed < date.today():
                expired.append(doc.original_filename)
    if expired:
        findings.append("Expired document(s): " + ", ".join(expired))
        actions.append("Upload current, unexpired versions of the flagged documents.")

    if settings.verification_mode == "manual":
        findings.append("External tax/company registry verification is not connected.")
        actions.append("Complete tax and company-registration checks using an authorized verification provider.")
    else:
        evidence.append("Demo provider mode: tax identifier was format-checked only; no government registry was queried.")

    if not findings:
        findings.append("Tax identifier format and document identity cross-references validated.")

    status = FAIL if hard_failure else (NEEDS if actions else PASS)
    if status == PASS:
        summary = "Tax identifier format and document identity details passed validation."
    elif status == FAIL:
        summary = "Tax identifier failed registry verification or is blocked."
    else:
        summary = f"{len(actions)} identity or tax validation action(s) required."

    return {
        "stage": "IDENTITY_TAX_VALIDATION",
        "stage_order": 2,
        "status": status,
        "findings": findings,
        "evidence": evidence,
        "required_actions": actions,
        "summary": summary,
    }


def financial_compliance(vendor: VendorSubmission, docs: list[VendorDocument]) -> dict:
    findings, evidence, actions = [], [], []
    doc_map = _docs_by_type(docs)

    if "bank_proof" in doc_map:
        bank_docs = doc_map["bank_proof"]
        for doc in bank_docs:
            extracted_holder = (doc.extraction_json or {}).get("fields", {}).get("bank_account_holder")
            if extracted_holder and name_similarity(vendor.bank_account_holder, str(extracted_holder)) < 0.72:
                findings.append(f"Bank account holder in {doc.original_filename} conflicts with the submitted bank account holder.")
                actions.append("Provide bank proof showing the same account holder, or explain the legal relationship and provide independent evidence.")
            elif extracted_holder:
                evidence.append(f"Account holder on {doc.original_filename} matches the submitted bank account holder.")
            extracted_ifsc = (doc.extraction_json or {}).get("fields", {}).get("ifsc")
            if extracted_ifsc and vendor.bank_country.upper() == "IN" and str(extracted_ifsc).upper() != re.sub(r"[^A-Z0-9]", "", vendor.bank_code.upper()):
                findings.append(f"IFSC on {doc.original_filename} ({extracted_ifsc}) differs from the submitted bank code ({vendor.bank_code}).")
                actions.append("Confirm the bank routing code shown on the bank proof.")
            elif extracted_ifsc:
                evidence.append(f"IFSC on {doc.original_filename} matches the submitted bank code.")

    holder_score = name_similarity(vendor.legal_name, vendor.bank_account_holder)
    if holder_score < 0.72:
        findings.append(f"Bank account holder ('{vendor.bank_account_holder}') does not sufficiently match the vendor legal name.")
        actions.append("Provide independent bank-account ownership evidence or a bank confirmation letter. Changes must be verified using a contact method already on file.")
    else:
        evidence.append(f"Bank account holder name similarity to legal entity: {holder_score:.0%} (heuristic; not bank ownership verification).")

    if vendor.bank_country.upper() != vendor.country_code.upper():
        findings.append(f"Bank country {vendor.bank_country} differs from vendor registration country {vendor.country_code}.")
        actions.append("Confirm why the supplier uses a bank account in a different country and provide supporting evidence.")

    routing_patterns = {
        "IN": r"^[A-Z]{4}0[A-Z0-9]{6}$",  # IFSC
        "US": r"^[0-9]{9}$",              # ABA routing number
        "GB": r"^[0-9]{6}$",              # sort code, digits only
        "AU": r"^[0-9]{6}$",              # BSB, digits only
        "SG": r"^[0-9]{3}-?[0-9]{4}$",    # illustrative bank/branch routing form
    }
    routing_pattern = routing_patterns.get(vendor.bank_country.upper())
    if routing_pattern and not re.fullmatch(routing_pattern, re.sub(r"[^A-Z0-9]", "", vendor.bank_code.upper())):
        findings.append(f"Bank routing code does not match the expected local format for {vendor.bank_country}.")
        actions.append("Correct the bank routing code or provide confirmation from the bank.")

    if not vendor.bank_reference.strip():
        findings.append("Bank verification reference is missing.")
        actions.append("Provide a verification reference from an approved bank-account verification provider.")

    if settings.verification_mode == "manual":
        findings.append("Independent bank-account verification has not been completed by a connected provider.")
        actions.append("Complete bank-account ownership verification with an approved provider or finance reviewer.")
    else:
        evidence.append("Demo provider mode: bank ownership is simulated and has NOT been independently verified.")

    if not findings:
        findings.append("Bank details passed local consistency checks.")

    status = NEEDS if actions else PASS
    if status == PASS:
        summary = "Bank account holder and routing information verified."
    else:
        summary = f"{len(actions)} financial compliance issue(s) identified."

    return {
        "stage": "FINANCIAL_COMPLIANCE",
        "stage_order": 3,
        "status": status,
        "findings": findings,
        "evidence": evidence,
        "required_actions": actions,
        "summary": summary,
    }


def business_procurement(db: Session, vendor: VendorSubmission, documents: list[VendorDocument] | None = None) -> dict:
    findings, evidence, actions = [], [], []
    if not vendor.business_justification.strip():
        findings.append("Business justification is missing.")
        actions.append("Provide the business purpose for engaging this supplier.")
    if not vendor.supplier_category.strip():
        findings.append("Supplier category is missing.")
        actions.append("Select the supplier category.")

    candidates = db.scalars(select(ApprovedVendor).where(ApprovedVendor.country_code == vendor.country_code)).all()
    duplicate = None
    for candidate in candidates:
        same_tax = candidate.tax_id_type == vendor.tax_id_type and candidate.tax_id.upper() == vendor.tax_id.upper()
        similar = name_similarity(candidate.legal_name, vendor.legal_name) >= 0.92
        if same_tax or similar:
            duplicate = candidate
            break

    # Also detect an earlier non-rejected application with the same tax identifier.
    prior = db.scalars(select(VendorSubmission).where(
        VendorSubmission.id != vendor.id,
        VendorSubmission.country_code == vendor.country_code,
        VendorSubmission.tax_id == vendor.tax_id,
        VendorSubmission.status != "REJECTED",
    )).first()
    if duplicate or prior:
        matched_name = duplicate.legal_name if duplicate else prior.legal_name
        findings.append(f"Potential duplicate supplier record found: {matched_name}.")
        evidence.append("Exact tax identifier or high legal-name similarity matched an existing record.")
        actions.append("Review the existing supplier record and confirm whether this is the same legal entity; do not create a second record automatically.")

    if not findings:
        findings.append("Supplier category, business purpose and duplicate checks passed.")
        evidence.append("Procurement checks were evaluated against the submitted profile and local supplier master.")

    status = NEEDS if actions else PASS
    if status == PASS:
        summary = "Business justification, supplier category, and duplicate checks passed."
    else:
        summary = f"{len(actions)} procurement action(s) required."

    return {
        "stage": "BUSINESS_PROCUREMENT",
        "stage_order": 4,
        "status": status,
        "findings": findings,
        "evidence": evidence,
        "required_actions": actions,
        "summary": summary,
    }


# Backwards compatibility aliases
procurement_review = business_procurement
finance_review = financial_compliance
compliance_review = identity_tax_validation


def evaluate_vendor(db: Session, vendor: VendorSubmission) -> tuple[str, list[dict], list[str]]:
    docs = list(vendor.documents)
    reviews = [
        document_verification(docs),
        identity_tax_validation(vendor, docs),
        financial_compliance(vendor, docs),
        business_procurement(db, vendor, docs),
    ]
    all_actions = []
    for review in reviews:
        all_actions.extend(review["required_actions"])
    if any(r["status"] == FAIL for r in reviews):
        status = "REJECTED"
    elif any(r["status"] == NEEDS for r in reviews):
        status = "PENDING"
    else:
        status = "APPROVED"
    return status, reviews, list(dict.fromkeys(all_actions))

