"""VendorIQ — Streamlit front end.

Runs the existing FastAPI backend (backend/app) in-process and talks to it through
FastAPI's TestClient, so the Streamlit UI and the React UI share exactly the same
pipeline: OCR/extraction, 5-stage verification, Gemini risk scoring and emails.
"""
from __future__ import annotations

import html
import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

st.set_page_config(page_title="VendorIQ", page_icon="🏢", layout="wide")

# Streamlit Cloud secrets → environment variables, before the backend reads its settings.
try:
    for _key, _value in st.secrets.items():
        if isinstance(_value, (str, int, float, bool)):
            os.environ.setdefault(_key.upper(), str(_value))
except Exception:
    pass  # no secrets file locally — environment variables / defaults apply


@st.cache_resource
def get_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


api = get_client()
from app.services.email_service import STAGE_META, STAGE_ORDER, _decision_headline, stage_outcome  # noqa: E402

DOC_SLOTS = [
    ("registration_certificate", "Company registration certificate", "Certificate of incorporation or business registration"),
    ("tax_certificate", "Tax registration certificate", "GST, PAN, VAT, EIN or equivalent certificate"),
    ("bank_proof", "Bank proof", "Bank letter, statement or cancelled cheque"),
]
FILE_TYPES = ["pdf", "png", "jpg", "jpeg", "tiff", "bmp", "txt", "csv"]
TAX_OPTIONS = {"IN": ["GSTIN", "PAN"], "US": ["EIN"], "GB": ["VAT"], "AU": ["ABN"], "SG": ["UEN"]}
COUNTRIES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "AU": "Australia", "SG": "Singapore"}
CATEGORIES = ["Industrial supplies", "IT services", "Professional services", "Logistics", "Facilities", "Raw materials", "Other"]
FORM_FIELDS = [
    "legal_name", "country_code", "tax_id_type", "tax_id", "registration_id", "registered_address",
    "contact_name", "contact_email", "supplier_category", "business_justification",
    "bank_account_holder", "bank_country", "bank_code", "bank_reference",
]
STATUS_STYLE = {
    "APPROVED": ("#1f8a5c", "#eaf8f1", "✅"),
    "PENDING": ("#a26f17", "#fff7e6", "⏳"),
    "REJECTED": ("#b4433a", "#fff0ef", "❌"),
    "SUBMITTED": ("#52647d", "#eef3fa", "📨"),
}
STAGE_STYLE = {
    "PASS": ("#1f8a5c", "#eaf8f1", "✓", "Passed"),
    "FAIL": ("#b4433a", "#fff0ef", "✕", "Failed"),
    "NEEDS_INFORMATION": ("#a26f17", "#fff7e6", "!", "Needs info"),
    "NOT_REACHED": ("#94a3b8", "#f1f5f9", "·", "Not run"),
}

st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; max-width: 1250px;}
      .viq-pill {display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700}
      .viq-verdict {border-radius:10px;padding:12px 14px;margin:6px 0 14px;border:1px solid}
      .viq-verdict b {display:block;font-size:15px}
      .viq-verdict span {font-size:13px;opacity:.9}
      .viq-track {display:flex;align-items:flex-start;gap:0;margin:8px 0 6px;flex-wrap:wrap}
      .viq-node {flex:1;min-width:110px;text-align:center}
      .viq-dot {width:42px;height:42px;border-radius:50%;margin:0 auto 6px;display:flex;align-items:center;
                justify-content:center;font-weight:800;font-size:18px;border:2px solid}
      .viq-node small {display:block;font-size:12px;font-weight:700;color:#334155}
      .viq-node em {display:block;font-style:normal;font-size:11px;font-weight:700;margin-top:2px}
      .viq-node.blocking .viq-dot {box-shadow:0 0 0 5px rgba(180,67,58,.15)}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── API helpers ────────────────────────────────────────────────────────────────

def call(method: str, path: str, **kwargs):
    response = api.request(method, path, **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(str(detail))
    return response.json()


def upload(submission_id: int, file, doc_type: str):
    return call(
        "POST", f"/api/submissions/{submission_id}/documents",
        data={"document_type": doc_type},
        files={"file": (file.name, file.getvalue(), file.type or "application/octet-stream")},
    )


def status_pill(status: str) -> str:
    fg, bg, icon = STATUS_STYLE.get(status, STATUS_STYLE["SUBMITTED"])
    return f'<span class="viq-pill" style="color:{fg};background:{bg}">{icon} {html.escape(status.title())}</span>'


def risk_text(score, level) -> str:
    return "—" if score is None else f"{score} {level or ''}".strip()


def open_application(submission_id: int):
    st.session_state.selected_id = submission_id
    st.session_state.page = "Applications"


# ── Pipeline view (same decision wording as the emails) ───────────────────────

def render_pipeline(item: dict):
    reviews = item.get("reviews") or item.get("pipeline_stages") or []
    if not reviews:
        st.info("Not evaluated yet — click **Run checks** to send this application through the 5-stage pipeline.")
        return
    outcome = stage_outcome(reviews)
    blocking = outcome["failed"] or (outcome["held"][0] if outcome["held"] else None)
    status = item["status"]

    headline = _decision_headline(status, outcome)
    fg, bg, _ = STATUS_STYLE.get(status, STATUS_STYLE["SUBMITTED"])
    detail = ""
    if blocking and blocking.get("findings"):
        detail = blocking["findings"][0]
    elif status == "APPROVED":
        detail = "Every verification stage passed and the vendor was approved."
    st.markdown(
        f'<div class="viq-verdict" style="color:{fg};background:{bg};border-color:{fg}33">'
        f"<b>{html.escape(headline)}</b><span>{html.escape(detail)}</span></div>",
        unsafe_allow_html=True,
    )

    by_key = {r["stage"]: r for r in reviews}
    nodes = []
    for key in STAGE_ORDER:
        stage_status = by_key.get(key, {}).get("status", "NOT_REACHED")
        sfg, sbg, mark, label = STAGE_STYLE.get(stage_status, STAGE_STYLE["NOT_REACHED"])
        is_blocking = blocking is not None and blocking["stage"] == key
        nodes.append(
            f'<div class="viq-node{" blocking" if is_blocking else ""}">'
            f'<div class="viq-dot" style="color:{sfg};background:{sbg};border-color:{sfg}">{mark}</div>'
            f'<small>{STAGE_META[key]["order"]}. {html.escape(STAGE_META[key]["label"])}</small>'
            f'<em style="color:{sfg}">{label}</em></div>'
        )
    st.markdown(f'<div class="viq-track">{"".join(nodes)}</div>', unsafe_allow_html=True)

    for key in STAGE_ORDER:
        review = by_key.get(key)
        if not review:
            continue
        _, _, mark, label = STAGE_STYLE.get(review["status"], STAGE_STYLE["NOT_REACHED"])
        is_blocking = blocking is not None and blocking["stage"] == key
        title = f"{STAGE_META[key]['order']}. {STAGE_META[key]['label']} — {label}"
        if is_blocking:
            title += "  ← failed here" if review["status"] == "FAIL" else "  ← on hold here"
        with st.expander(title, expanded=is_blocking):
            st.markdown("**Findings**")
            for finding in review.get("findings") or ["No issues found in this stage."]:
                st.markdown(f"- {finding}")
            if review.get("required_actions"):
                st.markdown("**Required actions**")
                for i, action in enumerate(review["required_actions"], 1):
                    st.markdown(f"{i}. {action}")
            if review.get("evidence"):
                st.caption(" · ".join(review["evidence"]))


# ── Pages ─────────────────────────────────────────────────────────────────────

def page_applications():
    selected_id = st.session_state.get("selected_id")
    if selected_id:
        return page_detail(selected_id)

    st.title("Vendor applications")
    stats = call("GET", "/api/dashboard/stats")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total applications", stats["total"])
    c2.metric("Pending review", stats["by_status"].get("PENDING", 0))
    c3.metric("Approved vendors", stats["by_status"].get("APPROVED", 0))
    c4.metric("Documents processed", stats["documents"])

    items = call("GET", "/api/submissions")
    search = st.text_input("Search vendors", placeholder="Search by name or email…", label_visibility="collapsed")
    if search:
        items = [i for i in items if search.lower() in (i["legal_name"] + i["contact_email"]).lower()]
    if not items:
        st.info("No applications yet. Use **New submission** in the sidebar to create one.")
        return

    head = st.columns([4, 1, 2, 1.4, 1.4, 1, 1])
    for col, label in zip(head, ["Supplier", "Country", "Submitted", "AI risk", "Status", "Docs", ""]):
        col.caption(label)
    for item in items:
        row = st.columns([4, 1, 2, 1.4, 1.4, 1, 1])
        row[0].markdown(f"**{html.escape(item['legal_name'])}**  \n<small>{html.escape(item['contact_email'])}</small>", unsafe_allow_html=True)
        row[1].write(item["country_code"])
        row[2].write(item["submitted_at"][:10])
        row[3].write(risk_text(item.get("ai_risk_score"), item.get("ai_risk_level")))
        row[4].markdown(status_pill(item["status"]), unsafe_allow_html=True)
        row[5].write(len(item.get("documents") or []))
        row[6].button("Open", key=f"open-{item['id']}", on_click=open_application, args=(item["id"],))


def page_detail(submission_id: int):
    try:
        item = call("GET", f"/api/submissions/{submission_id}")
    except RuntimeError as exc:
        st.error(str(exc))
        st.session_state.selected_id = None
        return

    top = st.columns([6, 1.3, 1.3])
    if top[0].button("← All applications"):
        st.session_state.selected_id = None
        st.rerun()
    top[1].markdown(f"**AI risk:** {risk_text(item.get('ai_risk_score'), item.get('ai_risk_level'))}")
    top[2].markdown(status_pill(item["status"]), unsafe_allow_html=True)

    st.title(item["legal_name"])
    st.caption(f"Application #{item['id']} · {item['supplier_category']} · {item['country_code']} · {item['contact_email']}")

    main, side = st.columns([2.2, 1])
    with main:
        with st.container(border=True):
            st.subheader("Verification pipeline")
            render_pipeline(item)
            if item.get("ai_detail_notes"):
                with st.expander("Gemini detailed analysis"):
                    st.write(item["ai_detail_notes"])
            if item["status"] != "APPROVED":
                if st.button("🔄 Run checks", type="primary"):
                    with st.spinner("Running the 5-stage pipeline and Gemini scoring…"):
                        try:
                            call("POST", f"/api/submissions/{submission_id}/evaluate")
                        except RuntimeError as exc:
                            st.error(str(exc))
                    st.rerun()

        with st.container(border=True):
            st.subheader("Documents")
            for doc in item.get("documents") or []:
                st.markdown(f"📄 **{html.escape(doc['original_filename'])}** — {doc['document_type'].replace('_', ' ')} · {doc['extraction_method']}")
            if not item.get("documents"):
                st.caption("No documents uploaded.")
            if item["status"] != "APPROVED":
                with st.form(f"more-docs-{submission_id}", clear_on_submit=True):
                    doc_type = st.selectbox("Document type", [s[0] for s in DOC_SLOTS], format_func=lambda k: dict((s[0], s[1]) for s in DOC_SLOTS)[k])
                    file = st.file_uploader("File", type=FILE_TYPES)
                    if st.form_submit_button("Upload document") and file:
                        try:
                            upload(submission_id, file, doc_type)
                            st.success("Uploaded. Click **Run checks** to re-evaluate.")
                        except RuntimeError as exc:
                            st.error(str(exc))
                        st.rerun()

    with side:
        with st.container(border=True):
            st.caption("FINAL DECISION")
            st.markdown(f"### {STATUS_STYLE.get(item['status'], STATUS_STYLE['SUBMITTED'])[2]} {item['status']}")
            outcome = stage_outcome(item.get("reviews") or [])
            if item.get("reviews"):
                st.markdown(f"**{_decision_headline(item['status'], outcome)}**")
            if item.get("demo_mode"):
                st.caption("Demo checks are not authoritative registry/bank verification.")

        comms = item.get("communications") or []
        if comms:
            comm = comms[-1]
            with st.container(border=True):
                st.caption("EMAIL NOTIFICATION")
                st.markdown(f"**{'Email sent ✓' if comm['sent'] else 'Draft (not sent)'}** to {html.escape(comm['recipient'])}")
                st.markdown(f"*{html.escape(comm['subject'])}*")
                if comm["body"].lstrip().startswith("<"):
                    # st.html sanitises the markup (no scripts); the body includes vendor-supplied text.
                    with st.container(height=520):
                        st.html(comm["body"])
                else:
                    st.text(comm["body"])

        with st.container(border=True):
            st.caption("SUBMITTED PROFILE")
            st.markdown(
                f"**Address:** {item['registered_address']}  \n"
                f"**Contact:** {item['contact_name']} · {item['contact_email']}  \n"
                f"**Tax ID:** {item['tax_id_type']} · {item['tax_id']}  \n"
                f"**Bank holder:** {item['bank_account_holder']}  \n"
                f"**Bank:** {item['bank_country']} · {item['bank_code']}  \n"
                f"**Business purpose:** {item['business_justification']}"
            )

        with st.expander("Audit history"):
            for row in call("GET", f"/api/submissions/{submission_id}/audit"):
                st.markdown(f"**{row['action'].replace('_', ' ')}**  \n<small>{row['actor']} · {str(row['created_at'])[:19]}</small>", unsafe_allow_html=True)


def load_sample(variant: int):
    sample = call("GET", f"/api/demo/sample?variant={variant}")
    for key in FORM_FIELDS:
        st.session_state[f"f_{key}"] = sample.get(key, "")
    st.session_state.sample_notice = (
        "Demo data 2 (BluePeak) loaded. Upload the BluePeak certificate of incorporation, GST certificate and "
        "cancelled cheque (samples/bluepeak/) to pass all checks."
        if variant == 2 else
        "Demo data 1 loaded. Add the three documents below or submit without them."
    )


def page_new_submission():
    st.title("New vendor submission")
    b1, b2, _ = st.columns([1, 1, 4])
    b1.button("Load demo data 1", on_click=load_sample, args=(1,), use_container_width=True)
    b2.button("Load demo data 2", on_click=load_sample, args=(2,), use_container_width=True,
              help="BluePeak Industrial Solutions — matches the BluePeak documents")
    if st.session_state.get("sample_notice"):
        st.success(st.session_state.sample_notice)

    for key in FORM_FIELDS:
        st.session_state.setdefault(f"f_{key}", "")
    if st.session_state.f_country_code not in COUNTRIES:
        st.session_state.f_country_code = "IN"
    if st.session_state.f_bank_country not in COUNTRIES:
        st.session_state.f_bank_country = "IN"

    with st.form("new-vendor"):
        st.markdown("##### 1 · Company information")
        c1, c2 = st.columns(2)
        c1.text_input("Legal company name *", key="f_legal_name")
        c2.selectbox("Country of registration *", list(COUNTRIES), format_func=COUNTRIES.get, key="f_country_code")
        c1, c2 = st.columns(2)
        all_tax_types = sorted({t for types in TAX_OPTIONS.values() for t in types})
        if st.session_state.f_tax_id_type not in all_tax_types:
            st.session_state.f_tax_id_type = TAX_OPTIONS[st.session_state.f_country_code][0]
        c1.selectbox("Tax identifier type *", all_tax_types, key="f_tax_id_type",
                     help="IN: GSTIN or PAN · US: EIN · GB: VAT · AU: ABN · SG: UEN")
        c2.text_input("Tax identifier *", key="f_tax_id")
        c1, c2 = st.columns(2)
        c1.text_input("Company registration ID", key="f_registration_id", placeholder="CIN / company number")
        if st.session_state.f_supplier_category and st.session_state.f_supplier_category not in CATEGORIES:
            st.session_state.f_supplier_category = "Other"
        c2.selectbox("Supplier category *", [""] + CATEGORIES, key="f_supplier_category")
        st.text_area("Registered business address *", key="f_registered_address")
        st.text_area("Business justification *", key="f_business_justification")

        st.markdown("##### 2 · Primary contact")
        c1, c2 = st.columns(2)
        c1.text_input("Contact name *", key="f_contact_name")
        c2.text_input("Business email *", key="f_contact_email")

        st.markdown("##### 3 · Payment details")
        c1, c2 = st.columns(2)
        c1.text_input("Bank account holder *", key="f_bank_account_holder")
        c2.selectbox("Bank country *", list(COUNTRIES), format_func=COUNTRIES.get, key="f_bank_country")
        c1, c2 = st.columns(2)
        c1.text_input("Bank routing code (IFSC / sort code / ABA) *", key="f_bank_code")
        c2.text_input("Bank verification reference *", key="f_bank_reference")

        st.markdown("##### 4 · Supporting documents")
        files = {}
        for key, label, hint in DOC_SLOTS:
            files[key] = st.file_uploader(label, type=FILE_TYPES, help=hint, key=f"file_{key}")

        submitted = st.form_submit_button("Submit & evaluate", type="primary")

    if not submitted:
        return
    payload = {k: (st.session_state[f"f_{k}"] or "").strip() for k in FORM_FIELDS}
    payload["registration_id"] = payload["registration_id"] or None
    missing = [k for k in FORM_FIELDS if k != "registration_id" and not payload[k]]
    if missing:
        st.error("Please fill in: " + ", ".join(m.replace("_", " ") for m in missing))
        return
    with st.status("Submitting and evaluating…", expanded=True) as status:
        try:
            st.write("Creating application…")
            created = call("POST", "/api/submissions", json=payload)
            for key, label, _ in DOC_SLOTS:
                if files[key]:
                    st.write(f"Reading {label.lower()}…")
                    upload(created["id"], files[key], key)
            st.write("Running 5-stage pipeline and Gemini risk scoring…")
            call("POST", f"/api/submissions/{created['id']}/evaluate")
        except RuntimeError as exc:
            status.update(label="Submission failed", state="error")
            st.error(str(exc))
            return
        status.update(label="Done", state="complete")
    for key in FORM_FIELDS:
        st.session_state.pop(f"f_{key}", None)
    st.session_state.pop("sample_notice", None)
    open_application(created["id"])
    st.rerun()


def page_admin():
    st.title("Admin dashboard")
    data = call("GET", "/api/admin/stats")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", data["total"])
    c2.metric("Approved", data["by_status"].get("APPROVED", 0))
    c3.metric("Pending", data["by_status"].get("PENDING", 0))
    c4.metric("Rejected", data["by_status"].get("REJECTED", 0))
    st.caption(
        f"Gemini {'enabled' if data['gemini_enabled'] else 'disabled'} · "
        f"score ≤{data['ai_low_threshold']} auto-approve · ≥{data['ai_high_threshold']} auto-reject · "
        f"email {'SMTP' if data['email_enabled'] else 'simulated (demo)'}"
    )

    queue = [s for s in data["submissions"] if s["status"] in ("PENDING", "SUBMITTED")]
    st.subheader(f"Manual review queue ({len(queue)})")
    if not queue:
        st.info("Nothing waiting for review.")
    for sub in queue:
        with st.container(border=True):
            head = st.columns([5, 1.5, 1.5])
            head[0].markdown(f"**#{sub['id']} · {html.escape(sub['legal_name'])}**  \n<small>{html.escape(sub['contact_email'])}</small>", unsafe_allow_html=True)
            head[1].markdown(f"AI risk: **{risk_text(sub.get('ai_risk_score'), sub.get('ai_risk_level'))}**")
            head[2].markdown(status_pill(sub["status"]), unsafe_allow_html=True)
            render_pipeline(sub)
            reason = st.text_input("Decision note (optional)", key=f"reason-{sub['id']}")
            a1, a2, a3, _ = st.columns([1, 1, 1, 3])
            for col, decision, label in ((a1, "APPROVED", "✅ Approve"), (a2, "REJECTED", "❌ Reject")):
                if col.button(label, key=f"{decision}-{sub['id']}"):
                    try:
                        result = call("PATCH", f"/api/admin/submissions/{sub['id']}/decision", json={"decision": decision, "reason": reason})
                        st.toast(result["message"])
                    except RuntimeError as exc:
                        st.error(str(exc))
                    st.rerun()
            a3.button("Open", key=f"admin-open-{sub['id']}", on_click=open_application, args=(sub["id"],))


# ── Layout ────────────────────────────────────────────────────────────────────

PAGES = {"Applications": page_applications, "New submission": page_new_submission, "Admin dashboard": page_admin}
st.session_state.setdefault("page", "Applications")

with st.sidebar:
    st.markdown("## 🏢 VendorIQ")
    st.caption("ONBOARDING CONTROL")
    for name in PAGES:
        if st.button(name, use_container_width=True, type="primary" if st.session_state.page == name else "secondary"):
            st.session_state.page = name
            st.session_state.selected_id = None
            st.rerun()
    health = call("GET", "/api/health")
    st.divider()
    st.caption(
        f"{'🟢 Gemini AI on' if health['gemini_enabled'] else '⚪ Gemini AI off (rules only)'}  \n"
        f"Mode: {health['verification_mode']} · Score ≤{health['ai_low_threshold']} approve · ≥{health['ai_high_threshold']} reject"
    )
    st.caption("Demo environment — not real registry or bank verification. Don't upload real personal or bank data.")

PAGES[st.session_state.page]()
