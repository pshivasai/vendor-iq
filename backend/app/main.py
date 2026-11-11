from __future__ import annotations
import os
import uuid
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, Depends, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session, selectinload
from .config import settings
from .database import Base, engine, get_db
from .models import (
    VendorSubmission, VendorDocument, CommunicationLog,
    AuditLog, ApprovedVendor, ReviewResult,
)
from .schemas import VendorSubmissionCreate, VendorSubmissionUpdate, VendorSubmissionOut
from .services.extraction import extract_document, DocumentExtractionError
from .services.gemini_risk import score_vendor_risk
from .services.email_service import (
    send_vendor_email, build_approval_email, build_rejection_email,
    build_manual_review_email,
)

Base.metadata.create_all(bind=engine)
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)


def _run_db_migrations():
    """Add new columns to existing DB without dropping data (SQLite-safe)."""
    migrations = [
        "ALTER TABLE vendor_submissions ADD COLUMN ai_risk_score INTEGER",
        "ALTER TABLE vendor_submissions ADD COLUMN ai_risk_level TEXT",
        "ALTER TABLE vendor_submissions ADD COLUMN ai_risk_reason TEXT",
        "ALTER TABLE vendor_submissions ADD COLUMN ai_detail_notes TEXT",
        "ALTER TABLE vendor_submissions ADD COLUMN vendor_fields_snapshot TEXT",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore


_run_db_migrations()

app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description=(
        "VendorIQ: supplier submission, document extraction, Gemini AI risk scoring "
        "with auto-accept / auto-reject / manual-review decisioning, admin dashboard and audit history."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_DOC_TYPES = {
    "registration_certificate", "tax_certificate", "bank_proof", "insurance_certificate",
    "gst_certificate", "pan_document", "pan_card", "vat_certificate", "ein_letter",
    "abn_certificate", "certificate_of_incorporation", "business_registration",
    "bank_statement", "void_cheque", "other",
}
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".txt", ".csv"}


def _audit(db: Session, submission_id: int, action: str, details: dict | None = None, actor: str = "system"):
    db.add(AuditLog(submission_id=submission_id, actor=actor, action=action, details=details or {}))


def _load_submission(db: Session, submission_id: int) -> VendorSubmission:
    stmt = (
        select(VendorSubmission)
        .where(VendorSubmission.id == submission_id)
        .options(
            selectinload(VendorSubmission.documents),
            selectinload(VendorSubmission.communications),
            selectinload(VendorSubmission.reviews),
        )
        # Session uses expire_on_commit=False; refresh so responses reflect rows just written.
        .execution_options(populate_existing=True)
    )
    vendor = db.scalar(stmt)
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor submission not found")
    return vendor


def _build_fields_snapshot(vendor: VendorSubmission) -> dict:
    """Capture a full snapshot of all vendor form fields for the detail view."""
    return {
        "legal_name": vendor.legal_name,
        "country_code": vendor.country_code,
        "tax_id_type": vendor.tax_id_type,
        "tax_id": vendor.tax_id,
        "registration_id": vendor.registration_id,
        "registered_address": vendor.registered_address,
        "contact_name": vendor.contact_name,
        "contact_email": vendor.contact_email,
        "supplier_category": vendor.supplier_category,
        "business_justification": vendor.business_justification,
        "bank_account_holder": vendor.bank_account_holder,
        "bank_country": vendor.bank_country,
        "bank_code": vendor.bank_code,
        "bank_reference": vendor.bank_reference,
    }


# ── Core API ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok", "service": "vendor-iq",
        "verification_mode": settings.verification_mode,
        "gemini_enabled": settings.gemini_enabled,
        "email_enabled": settings.email_enabled,
        "ai_low_threshold": settings.ai_low_threshold,
        "ai_high_threshold": settings.ai_high_threshold,
    }


@app.post("/api/submissions", response_model=VendorSubmissionOut, status_code=201)
def create_submission(payload: VendorSubmissionCreate, db: Session = Depends(get_db)):
    vendor = VendorSubmission(**payload.model_dump(), status="SUBMITTED", demo_mode=settings.verification_mode == "demo")
    db.add(vendor)
    db.flush()
    _audit(db, vendor.id, "SUBMISSION_CREATED", {"country_code": vendor.country_code, "tax_id_type": vendor.tax_id_type})
    db.commit()
    return _load_submission(db, vendor.id)


@app.get("/api/submissions", response_model=list[VendorSubmissionOut])
def list_submissions(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(VendorSubmission).options(
        selectinload(VendorSubmission.documents),
        selectinload(VendorSubmission.communications),
    ).order_by(VendorSubmission.submitted_at.desc())
    if status:
        stmt = stmt.where(VendorSubmission.status == status.upper())
    return list(db.scalars(stmt).all())


@app.get("/api/submissions/{submission_id}", response_model=VendorSubmissionOut)
def get_submission(submission_id: int, db: Session = Depends(get_db)):
    return _load_submission(db, submission_id)


@app.put("/api/submissions/{submission_id}", response_model=VendorSubmissionOut)
def update_submission(submission_id: int, payload: VendorSubmissionUpdate, db: Session = Depends(get_db)):
    vendor = _load_submission(db, submission_id)
    if vendor.status == "APPROVED":
        raise HTTPException(status_code=409, detail="Approved vendor profile is locked; use a controlled vendor-change workflow")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(vendor, key, value)
    vendor.status = "SUBMITTED"
    _audit(db, vendor.id, "SUBMISSION_UPDATED", {"changed_fields": sorted(changes.keys())}, actor="vendor")
    db.commit()
    return _load_submission(db, vendor.id)


@app.post("/api/submissions/{submission_id}/documents", response_model=VendorSubmissionOut)
async def upload_document(
    submission_id: int,
    file: Annotated[UploadFile, File(...)],
    document_type: Annotated[str, Form(...)],
    db: Session = Depends(get_db),
):
    vendor = _load_submission(db, submission_id)
    if vendor.status == "APPROVED":
        raise HTTPException(status_code=409, detail="Cannot add onboarding documents after approval")
    doc_type = document_type.strip().lower()
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported document_type. Allowed: {sorted(ALLOWED_DOC_TYPES)}")
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Supported files: PDF, PNG, JPG, TIFF, BMP, TXT and CSV")
    content = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB limit")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = Path(settings.upload_dir) / stored_name
    target.write_bytes(content)
    try:
        text_out, method, extraction = extract_document(target, file.content_type or "application/octet-stream", doc_type)
    except DocumentExtractionError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = VendorDocument(
        submission_id=vendor.id,
        original_filename=(file.filename or "upload")[:255],
        stored_filename=stored_name,
        content_type=file.content_type or "application/octet-stream",
        document_type=doc_type,
        extracted_text=text_out,
        extraction_json=extraction,
        extraction_method=method,
    )
    db.add(record)
    vendor.status = "SUBMITTED"
    _audit(db, vendor.id, "DOCUMENT_UPLOADED", {
        "filename": record.original_filename,
        "document_type": doc_type,
        "extraction_method": method,
        "extracted_text_characters": len(text_out),
    }, actor="vendor")
    db.commit()
    return _load_submission(db, vendor.id)


@app.post("/api/submissions/{submission_id}/evaluate", response_model=VendorSubmissionOut)
def evaluate_submission(submission_id: int, db: Session = Depends(get_db)):
    """
    Run 5-stage pipeline evaluation + Gemini AI scoring.
    - Score <= LOW_THRESHOLD  → AUTO APPROVED  → approval email sent
    - Score >= HIGH_THRESHOLD → AUTO REJECTED  → rejection email sent
    - Score in between        → PENDING        → manual review email sent
    """
    from .services.review import evaluate_vendor

    vendor = _load_submission(db, submission_id)
    if vendor.status == "APPROVED":
        raise HTTPException(status_code=409, detail="Vendor is already approved")

    documents = list(vendor.documents)

    # ── Run 4-stage pipeline review ───────────────────────────────────────────
    pipeline_status, pipeline_stages, _all_actions = evaluate_vendor(db, vendor)

    # ── Gemini AI risk scoring + decisioning ──────────────────────────────────
    risk = score_vendor_risk(vendor, documents)

    vendor.ai_risk_score = risk.get("risk_score")
    vendor.ai_risk_level = risk.get("risk_level")
    vendor.ai_risk_reason = risk.get("reason")
    vendor.ai_detail_notes = risk.get("detail_notes")
    vendor.vendor_fields_snapshot = _build_fields_snapshot(vendor)

    ai_status = risk.get("final_status", "PENDING")
    ai_scored = risk.get("risk_score") is not None
    # Deterministic rule checks are the floor: a failed check rejects, missing info holds.
    # Gemini only decides when every rule stage passes; without Gemini, the rules decide alone.
    if pipeline_status == "REJECTED":
        final_status = "REJECTED"
    elif pipeline_status == "PENDING":
        final_status = "REJECTED" if ai_status == "REJECTED" else "PENDING"
    elif ai_scored:
        final_status = ai_status
    else:
        final_status = pipeline_status
    vendor.status = final_status

    # ── Build the 5th AI_RISK_ASSESSMENT stage ────────────────────────────────
    ai_stage_status = "PASS" if final_status == "APPROVED" else ("FAIL" if final_status == "REJECTED" else "NEEDS_INFORMATION")
    ai_stage = {
        "stage": "AI_RISK_ASSESSMENT",
        "stage_order": 5,
        "status": ai_stage_status,
        "findings": [risk.get("reason", "AI scoring completed.")],
        "evidence": [f"Risk score: {risk.get('risk_score', 'N/A')}, Level: {risk.get('risk_level', 'N/A')}"],
        "required_actions": [],
        "summary": f"AI risk score: {risk.get('risk_score', 'N/A')} ({risk.get('risk_level', 'N/A')}) — {final_status.lower().replace('_', ' ')}.",
    }
    pipeline_stages.append(ai_stage)

    # ── Persist pipeline stages as ReviewResult rows ──────────────────────────
    # Delete old reviews for this submission to avoid duplicates
    for old_review in list(vendor.reviews):
        db.delete(old_review)
    db.flush()

    for stage in pipeline_stages:
        db.add(ReviewResult(
            submission_id=vendor.id,
            stage=stage["stage"],
            status=stage["status"],
            findings=stage.get("findings", []),
            evidence=stage.get("evidence", []),
            required_actions=stage.get("required_actions", []),
        ))

    # ── Auto-approve: add to ApprovedVendor registry ─────────────────────────
    if final_status == "APPROVED":
        existing = db.scalar(select(ApprovedVendor).where(
            ApprovedVendor.country_code == vendor.country_code,
            ApprovedVendor.tax_id_type == vendor.tax_id_type,
            ApprovedVendor.tax_id == vendor.tax_id,
        ))
        if not existing:
            db.add(ApprovedVendor(
                legal_name=vendor.legal_name,
                country_code=vendor.country_code,
                tax_id_type=vendor.tax_id_type,
                tax_id=vendor.tax_id,
                source_submission_id=vendor.id,
            ))

    # ── Send pipeline-aware email for ALL statuses ───────────────────────────
    email_sent = False
    if final_status == "APPROVED":
        subject, body_plain, body_html = build_approval_email(vendor, pipeline_stages)
        email_sent = send_vendor_email(vendor.contact_email, subject, body_plain, body_html)
        db.add(CommunicationLog(
            submission_id=vendor.id,
            recipient=vendor.contact_email,
            subject=subject,
            body=body_html,
            channel="EMAIL",
            sent=email_sent,
        ))
    elif final_status == "REJECTED":
        subject, body_plain, body_html = build_rejection_email(vendor, pipeline_stages)
        email_sent = send_vendor_email(vendor.contact_email, subject, body_plain, body_html)
        db.add(CommunicationLog(
            submission_id=vendor.id,
            recipient=vendor.contact_email,
            subject=subject,
            body=body_html,
            channel="EMAIL",
            sent=email_sent,
        ))
    else:
        # PENDING → send manual review notification email
        subject, body_plain, body_html = build_manual_review_email(vendor, pipeline_stages)
        email_sent = send_vendor_email(vendor.contact_email, subject, body_plain, body_html)
        db.add(CommunicationLog(
            submission_id=vendor.id,
            recipient=vendor.contact_email,
            subject=subject,
            body=body_html,
            channel="EMAIL",
            sent=email_sent,
        ))

    _audit(db, vendor.id, "GEMINI_EVALUATION_COMPLETED", {
        "final_status": final_status,
        "ai_risk_score": vendor.ai_risk_score,
        "ai_risk_level": vendor.ai_risk_level,
        "documents_included": len(documents),
        "pipeline_stages": len(pipeline_stages),
        "email_sent": email_sent,
    })
    db.commit()
    return _load_submission(db, vendor.id)


@app.get("/api/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    counts = {
        status: db.scalar(select(func.count()).select_from(VendorSubmission).where(VendorSubmission.status == status)) or 0
        for status in ["SUBMITTED", "PENDING", "APPROVED", "REJECTED"]
    }
    total = sum(counts.values())
    return {
        "total": total,
        "by_status": counts,
        "documents": db.scalar(select(func.count()).select_from(VendorDocument)) or 0,
        "mode": settings.verification_mode,
        "gemini_enabled": settings.gemini_enabled,
    }


# ── Admin endpoints ─────────────────────────────────────────────────────────

@app.get("/api/admin/stats")
def admin_stats(db: Session = Depends(get_db)):
    """Returns summary stats + all submissions with AI risk fields for the admin dashboard."""
    counts = {
        status: db.scalar(select(func.count()).select_from(VendorSubmission).where(VendorSubmission.status == status)) or 0
        for status in ["SUBMITTED", "PENDING", "APPROVED", "REJECTED"]
    }
    total = sum(counts.values())

    rows = db.scalars(
        select(VendorSubmission)
        .options(
            selectinload(VendorSubmission.communications),
            selectinload(VendorSubmission.documents),
            selectinload(VendorSubmission.reviews),
        )
        .order_by(VendorSubmission.submitted_at.desc())
    ).all()

    submissions = []
    for v in rows:
        latest_comm = v.communications[-1] if v.communications else None
        docs_summary = [
            {
                "id": d.id,
                "original_filename": d.original_filename,
                "document_type": d.document_type,
                "extraction_method": d.extraction_method,
                "extracted_fields": (d.extraction_json or {}).get("fields", {}),
            }
            for d in v.documents
        ]
        # Build pipeline stages from reviews
        pipeline_stages = sorted(
            [
                {
                    "stage": r.stage,
                    "status": r.status,
                    "findings": r.findings or [],
                    "evidence": r.evidence or [],
                    "required_actions": r.required_actions or [],
                }
                for r in v.reviews
            ],
            key=lambda s: {"DOCUMENT_VERIFICATION": 1, "IDENTITY_TAX_VALIDATION": 2, "FINANCIAL_COMPLIANCE": 3, "BUSINESS_PROCUREMENT": 4, "AI_RISK_ASSESSMENT": 5}.get(s["stage"], 99),
        )
        submissions.append({
            "id": v.id,
            "legal_name": v.legal_name,
            "supplier_category": v.supplier_category,
            "country_code": v.country_code,
            "contact_name": v.contact_name,
            "contact_email": v.contact_email,
            "status": v.status,
            "submitted_at": v.submitted_at.isoformat(),
            "ai_risk_score": v.ai_risk_score,
            "ai_risk_level": v.ai_risk_level,
            "ai_risk_reason": v.ai_risk_reason,
            "ai_detail_notes": v.ai_detail_notes,
            "vendor_fields_snapshot": v.vendor_fields_snapshot,
            "documents": docs_summary,
            "pipeline_stages": pipeline_stages,
            "latest_communication": {
                "recipient": latest_comm.recipient,
                "subject": latest_comm.subject,
                "body": latest_comm.body,
                "sent": latest_comm.sent,
                "created_at": latest_comm.created_at.isoformat(),
            } if latest_comm else None,
        })

    # Manual review queue: PENDING with MEDIUM risk level
    manual_queue = [s for s in submissions if s["status"] == "PENDING" and s["ai_risk_level"] == "MEDIUM"]

    return {
        "total": total,
        "by_status": counts,
        "submissions": submissions,
        "manual_review_queue": manual_queue,
        "gemini_enabled": settings.gemini_enabled,
        "email_enabled": settings.email_enabled,
        "ai_low_threshold": settings.ai_low_threshold,
        "ai_high_threshold": settings.ai_high_threshold,
    }


@app.patch("/api/admin/submissions/{submission_id}/decision")
def admin_decision(submission_id: int, body: dict, db: Session = Depends(get_db)):
    """Allow admin to manually approve or reject any PENDING or SUBMITTED application.
    Sends an automatic pipeline-aware email on decision."""
    vendor = _load_submission(db, submission_id)
    decision = str(body.get("decision", "")).upper()
    reason_override = str(body.get("reason", "")).strip()

    if decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=422, detail="decision must be APPROVED or REJECTED")
    if vendor.status == "APPROVED":
        raise HTTPException(status_code=409, detail="Vendor is already approved")

    vendor.status = decision
    if reason_override:
        vendor.ai_risk_reason = reason_override

    _audit(db, vendor.id, "ADMIN_DECISION", {"decision": decision}, actor="admin")

    # Gather existing pipeline stages for email
    pipeline_stages = [
        {
            "stage": r.stage,
            "status": r.status,
            "findings": r.findings or [],
            "evidence": r.evidence or [],
            "required_actions": r.required_actions or [],
            "summary": ", ".join(r.findings or [])[:80],
        }
        for r in sorted(vendor.reviews, key=lambda r: r.created_at)
    ]

    if decision == "APPROVED":
        existing = db.scalar(select(ApprovedVendor).where(
            ApprovedVendor.country_code == vendor.country_code,
            ApprovedVendor.tax_id_type == vendor.tax_id_type,
            ApprovedVendor.tax_id == vendor.tax_id,
        ))
        if not existing:
            db.add(ApprovedVendor(
                legal_name=vendor.legal_name,
                country_code=vendor.country_code,
                tax_id_type=vendor.tax_id_type,
                tax_id=vendor.tax_id,
                source_submission_id=vendor.id,
            ))
        subject, body_plain, body_html = build_approval_email(vendor, pipeline_stages)
    else:
        subject, body_plain, body_html = build_rejection_email(vendor, pipeline_stages)

    # Send pipeline-aware email for admin decisions too
    email_sent = send_vendor_email(vendor.contact_email, subject, body_plain, body_html)
    db.add(CommunicationLog(
        submission_id=vendor.id,
        recipient=vendor.contact_email,
        subject=subject,
        body=body_html,
        channel="EMAIL",
        sent=email_sent,
    ))

    db.commit()
    return {
        "id": vendor.id,
        "status": vendor.status,
        "email_sent": email_sent,
        "message": f"Application #{submission_id} manually {decision} by admin.",
    }


# ── Misc endpoints ───────────────────────────────────────────────────────────

@app.get("/api/submissions/{submission_id}/audit")
def audit_history(submission_id: int, db: Session = Depends(get_db)):
    _load_submission(db, submission_id)
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.submission_id == submission_id)
        .order_by(AuditLog.created_at.asc())
    ).all()
    return [
        {"id": row.id, "actor": row.actor, "action": row.action,
         "details": row.details, "created_at": row.created_at}
        for row in rows
    ]


@app.get("/api/demo/sample")
def sample_payload():
    return {
        "legal_name": "Aarav Industrial Supplies Private Limited",
        "country_code": "IN", "tax_id_type": "GSTIN", "tax_id": "29ABCDE1234F1Z5",
        "registration_id": "U27100KA2020PTC123456",
        "registered_address": "Plot 12, Peenya Industrial Area, Bengaluru, Karnataka, India",
        "contact_name": "Riya Sharma", "contact_email": "vendor@example.com",
        "supplier_category": "Industrial supplies",
        "business_justification": "Supplier for approved maintenance and plant operations requirements.",
        "bank_account_holder": "Aarav Industrial Supplies Private Limited",
        "bank_country": "IN", "bank_code": "HDFC0001234", "bank_reference": "DEMO-BANK-REF-001",
    }
