import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from './api';
import {
  Activity, ArrowRight, BadgeCheck, Building2, CheckCircle2, ChevronRight,
  ClipboardList, Clock3, FileCheck2, FileText, RefreshCw, ShieldAlert,
  UploadCloud, XCircle, AlertTriangle, Mail, Plus, Search, Copy, CircleHelp,
  LayoutDashboard, Sparkles, Eye, X, User, MapPin, CreditCard, Briefcase,
  FileSearch, Brain, AlertCircle,
} from 'lucide-react';

const initialForm = {
  legal_name: '', country_code: 'IN', tax_id_type: 'GSTIN', tax_id: '', registration_id: '',
  registered_address: '', contact_name: '', contact_email: '', supplier_category: '',
  business_justification: '', bank_account_holder: '', bank_country: 'IN', bank_code: '', bank_reference: '',
};
const documentSlots = [
  { key: 'registration_certificate', label: 'Company registration certificate', hint: 'Certificate of incorporation or business registration' },
  { key: 'tax_certificate', label: 'Tax registration certificate', hint: 'GST, PAN, VAT, EIN or equivalent certificate' },
  { key: 'bank_proof', label: 'Bank proof', hint: 'Bank letter, statement or void cheque; redact full account numbers' },
];
const taxOptions = {
  IN: [{ value: 'GSTIN', label: 'GSTIN' }, { value: 'PAN', label: 'PAN' }],
  US: [{ value: 'EIN', label: 'EIN' }],
  GB: [{ value: 'VAT', label: 'VAT number' }],
  AU: [{ value: 'ABN', label: 'ABN' }],
  SG: [{ value: 'UEN', label: 'UEN' }],
};

// ── Utility Components ────────────────────────────────────────────────────────

function StatusPill({ status }) {
  const config = {
    APPROVED: ['approved', 'Approved', CheckCircle2],
    PENDING: ['pending', 'Pending', Clock3],
    REJECTED: ['rejected', 'Rejected', XCircle],
    SUBMITTED: ['submitted', 'Submitted', Activity],
  }[status] || ['submitted', status || 'Unknown', CircleHelp];
  const Icon = config[2];
  return <span className={`status-pill ${config[0]}`}><Icon size={13}/>{config[1]}</span>;
}

function RiskBadge({ score, level }) {
  if (score == null) return <span className="risk-badge unknown">—</span>;
  const cls = level === 'HIGH' ? 'high' : level === 'LOW' ? 'low' : 'medium';
  return (
    <span className={`risk-badge ${cls}`}>
      <span className="risk-score-num">{score}</span>
      {level}
    </span>
  );
}

function Field({ label, required = false, children, hint }) {
  return <label className="field"><span className="field-label">{label}{required && <b className="required"> *</b>}</span>{children}{hint && <small>{hint}</small>}</label>;
}

function StatCard({ icon: Icon, label, value, tint }) {
  return <div className="stat-card"><div className={`stat-icon ${tint}`}><Icon size={18}/></div><div><div className="stat-value">{value ?? '—'}</div><div className="stat-label">{label}</div></div></div>;
}

// ── Pipeline Stage Config ─────────────────────────────────────────────────────

const PIPELINE_STAGES = [
  { key: 'DOCUMENT_VERIFICATION', label: 'Document Verification', icon: FileCheck2 },
  { key: 'IDENTITY_TAX_VALIDATION', label: 'Identity & Tax', icon: ShieldAlert },
  { key: 'FINANCIAL_COMPLIANCE', label: 'Financial', icon: CreditCard },
  { key: 'BUSINESS_PROCUREMENT', label: 'Procurement', icon: Briefcase },
  { key: 'AI_RISK_ASSESSMENT', label: 'AI Assessment', icon: Brain },
];

function getStageClass(status) {
  if (status === 'PASS') return 'pass';
  if (status === 'FAIL') return 'fail';
  if (status === 'NEEDS_INFORMATION') return 'pending';
  return 'inactive';
}

function getStageStatusLabel(status) {
  if (status === 'PASS') return 'Passed';
  if (status === 'FAIL') return 'Failed';
  if (status === 'NEEDS_INFORMATION') return 'Needs Info';
  return 'Pending';
}

function getStageStatusIcon(status) {
  if (status === 'PASS') return <CheckCircle2 size={10}/>;
  if (status === 'FAIL') return <XCircle size={10}/>;
  if (status === 'NEEDS_INFORMATION') return <Clock3 size={10}/>;
  return <CircleHelp size={10}/>;
}

// ── Pipeline outcome: which stage stopped the application ─────────────────────

function pipelineOutcome(reviews) {
  const byKey = Object.fromEntries((reviews || []).map((r) => [r.stage, r]));
  const stages = PIPELINE_STAGES.map((cfg, i) => ({
    ...cfg,
    order: i + 1,
    status: byKey[cfg.key]?.status || 'NOT_REACHED',
    findings: byKey[cfg.key]?.findings || [],
    evidence: byKey[cfg.key]?.evidence || [],
    required_actions: byKey[cfg.key]?.required_actions || [],
  }));
  const failed = stages.find((s) => s.status === 'FAIL') || null;
  const cutoff = failed ? failed.order : 99;
  const held = stages.filter((s) => s.status === 'NEEDS_INFORMATION' && s.order < cutoff);
  return { stages, failed, held, ran: (reviews || []).length > 0 };
}

const stageRef = (s) => `Stage ${s.order} of ${PIPELINE_STAGES.length} — ${s.label}`;

function decisionHeadline(status, outcome) {
  if (!outcome.ran) return { title: 'Not evaluated yet', detail: 'Run checks to send this application through the 5-stage pipeline.' };
  if (status === 'APPROVED') return { title: `All ${PIPELINE_STAGES.length} stages passed`, detail: 'Every verification stage passed and the vendor was approved.' };
  if (status === 'REJECTED' && outcome.failed) {
    return { title: `Failed at ${stageRef(outcome.failed)}`, detail: outcome.failed.findings[0] || 'This stage did not pass.', stage: outcome.failed };
  }
  if (outcome.held.length) {
    const first = outcome.held[0];
    const more = outcome.held.length - 1;
    return {
      title: `On hold at ${stageRef(first)}${more ? ` (+${more} more)` : ''}`,
      detail: first.findings[0] || 'This stage needs more information.',
      stage: first,
    };
  }
  if (status === 'REJECTED') return { title: 'Rejected', detail: 'The application did not pass verification.' };
  return { title: 'Awaiting manual review', detail: 'All automated stages finished; an admin will make the final decision.' };
}

// ── Pipeline Visualization Component ──────────────────────────────────────────

function PipelineVisualization({ reviews, pipelineStages, riskScore, riskLevel, riskReason, status }) {
  // Build stage list from reviews (submission API) or pipeline_stages (admin API)
  const source = reviews?.length ? reviews : (pipelineStages || []);
  const sourceKey = JSON.stringify(source.map((s) => [s.stage, s.status, s.findings]));
  const outcome = useMemo(() => pipelineOutcome(source), [sourceKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const stages = outcome.stages;
  const blockingIdx = useMemo(() => {
    const target = outcome.failed || outcome.held[0];
    return target ? target.order - 1 : null;
  }, [outcome]);

  // Open the stage that stopped the application, and re-open it after a re-run.
  const [selectedStage, setSelectedStage] = useState(blockingIdx);
  useEffect(() => { setSelectedStage(blockingIdx); }, [blockingIdx, sourceKey]);

  const headline = status ? decisionHeadline(status, outcome) : null;
  const activeStage = selectedStage != null ? stages[selectedStage] : null;

  return (
    <div className="pipeline-container">
      {/* Header */}
      <div className="pipeline-header">
        <div className="pipeline-header-left">
          <div className="pipeline-title-row">
            <Sparkles size={14} className="pipeline-sparkle-icon"/>
            <span className="pipeline-header-title">Verification Pipeline</span>
          </div>
          <span className="pipeline-header-sub">5-stage automated verification</span>
        </div>
        <div className="pipeline-header-right">
          <div className="pipeline-risk-row">
            <span className="pipeline-risk-label">Risk Score</span>
            <RiskBadge score={riskScore} level={riskLevel}/>
          </div>
          <div className="pipeline-threshold-chips">
            <span className="threshold-chip low">≤30 Auto-Approve</span>
            <span className="threshold-chip medium">31–69 Manual</span>
            <span className="threshold-chip high">≥70 Auto-Reject</span>
          </div>
        </div>
      </div>

      {/* Where the pipeline stopped */}
      {headline && outcome.ran && (
        <div className={`pipeline-verdict ${status === 'APPROVED' ? 'pass' : status === 'REJECTED' ? 'fail' : 'pending'}`}>
          {status === 'APPROVED' ? <CheckCircle2 size={16}/> : status === 'REJECTED' ? <XCircle size={16}/> : <Clock3 size={16}/>}
          <div><b>{headline.title}</b><span>{headline.detail}</span></div>
        </div>
      )}

      {/* AI Summary */}
      {riskReason && (
        <div className="pipeline-ai-summary-bar">
          <span className="ai-summary-tag">AI</span>
          <span className="ai-summary-text">{riskReason}</span>
        </div>
      )}

      {/* Pipeline Track */}
      <div className="pipeline-track">
        {stages.map((stage, idx) => {
          const cls = getStageClass(stage.status);
          const isSelected = selectedStage === idx;
          const StageIcon = stage.icon;
          return (
            <React.Fragment key={stage.key}>
              <button
                className={`pipeline-node ${cls} ${isSelected ? 'selected' : ''}`}
                onClick={() => setSelectedStage(isSelected ? null : idx)}
                title={`${stage.label}: ${getStageStatusLabel(stage.status)}`}
              >
                <div className="pipeline-node-icon">
                  <StageIcon size={20}/>
                  <span className="pipeline-status-badge">{getStageStatusIcon(stage.status)}</span>
                </div>
                <span className="pipeline-node-name">{stage.label}</span>
                <span className="pipeline-node-substatus">{getStageStatusLabel(stage.status)}</span>
              </button>
              {idx < stages.length - 1 && (
                <div className={`pipeline-connector ${getStageClass(stages[idx + 1].status === 'NOT_REACHED' ? 'NOT_REACHED' : stage.status)}`}/>
              )}
            </React.Fragment>
          );
        })}
      </div>

      {/* Expanded Stage Detail */}
      {activeStage && (
        <div className="pipeline-detail">
          <div className="pipeline-detail-header">
            <div className="pipeline-detail-title-group">
              <div className="pipeline-detail-icon"><activeStage.icon size={16}/></div>
              <div>
                <div className="pipeline-detail-name">{activeStage.label}</div>
                <div className="pipeline-detail-desc">Stage {(selectedStage || 0) + 1} of 5{selectedStage === blockingIdx && status && status !== 'APPROVED' ? (activeStage.status === 'FAIL' ? ' · this is where the application failed' : ' · this is where the application is on hold') : ''}</div>
              </div>
            </div>
            <span className={`pipeline-status-tag ${getStageClass(activeStage.status)}`}>
              {getStageStatusIcon(activeStage.status)} {getStageStatusLabel(activeStage.status)}
            </span>
          </div>

          {activeStage.status === 'NOT_REACHED' ? (
            <div className="pipeline-inactive-notice">
              <CircleHelp size={16}/> This stage has not been reached yet. A preceding stage needs to be resolved first.
            </div>
          ) : (
            <>
              {/* Findings */}
              <div className="pipeline-detail-section-title"><FileText size={12}/> Findings</div>
              {activeStage.findings.length > 0 ? (
                <ul className="pipeline-findings-list">
                  {activeStage.findings.map((f, i) => (
                    <li key={i} className={`finding-item ${getStageClass(activeStage.status)}`}>
                      <span className="finding-bullet">•</span> {f}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className={`pipeline-empty-findings ${getStageClass(activeStage.status)}`}>
                  <CheckCircle2 size={14}/> No issues found in this stage.
                </div>
              )}

              {/* Required Actions */}
              {activeStage.required_actions.length > 0 && (
                <div className="pipeline-actions-box">
                  <div className="pipeline-actions-title"><AlertTriangle size={11}/> Required Actions</div>
                  <ul className="pipeline-actions-list">
                    {activeStage.required_actions.map((a, i) => (
                      <li key={i} className="action-item">
                        <span className="action-num">{i + 1}</span> {a}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Evidence */}
              {activeStage.evidence.length > 0 && (
                <div className="pipeline-evidence-box">
                  <div className="pipeline-evidence-title"><FileSearch size={11}/> Evidence</div>
                  <ul className="pipeline-evidence-list">
                    {activeStage.evidence.map((e, i) => <li key={i}>{e}</li>)}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Email Preview Component ───────────────────────────────────────────────────

function EmailPreview({ communications }) {
  const latest = communications?.length ? communications[communications.length - 1] : null;
  if (!latest) {
    return (
      <div className="email-empty-state">
        <Mail size={24} className="email-empty-icon"/>
        <div className="email-empty-title">No Email Sent</div>
        <p>An email notification will be generated when the application is evaluated or a decision is made.</p>
      </div>
    );
  }

  const copyBody = async () => {
    try { await navigator.clipboard.writeText(latest.body); } catch {}
  };

  // Determine if body is HTML
  const isHtml = latest.body && latest.body.trim().startsWith('<');

  return (
    <div className="email-client-card">
      <div className="email-meta-card">
        <div className="email-meta-row">
          <div className="email-meta-field">
            <span className="email-meta-label">To</span>
            <span className="email-meta-val">{latest.recipient}</span>
          </div>
          <div className="email-meta-actions">
            <span className={`email-sent-tag ${latest.sent ? 'sent' : 'draft'}`}>
              {latest.sent ? '✓ Sent' : 'Draft'}
            </span>
            <button className="btn-copy-email" onClick={copyBody}><Copy size={10}/> Copy</button>
          </div>
        </div>
        <div className="email-meta-row email-meta-subject-row">
          <span className="email-meta-label">Subject</span>
          <span className="email-meta-subject">{latest.subject}</span>
        </div>
        {latest.created_at && (
          <div className="email-meta-row email-meta-date-row">
            <span className="email-meta-label">Date</span>
            <span className="email-meta-date">{new Date(latest.created_at).toLocaleString()}</span>
          </div>
        )}
      </div>
      <div className="email-preview-frame-wrapper">
        <div className="email-frame-browser-bar">
          <div className="browser-dots">
            <span className="browser-dot red"/>
            <span className="browser-dot yellow"/>
            <span className="browser-dot green"/>
          </div>
          <span className="browser-url-bar">mail.vendoriq.local</span>
        </div>
        {isHtml ? (
          <iframe
            className="email-preview-frame"
            sandbox="allow-same-origin"
            srcDoc={latest.body}
            title="Email Preview"
          />
        ) : (
          <div style={{ padding: '16px', fontSize: '12px', color: '#334155', whiteSpace: 'pre-wrap', lineHeight: 1.6, maxHeight: 270, overflow: 'auto' }}>
            {latest.body}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Detail Side Panel ─────────────────────────────────────────────────────────

function DetailPanel({ item, onClose, onDecide, busy }) {
  if (!item) return null;

  const fields = item.vendor_fields_snapshot || {};
  const isManual = item.status === 'PENDING';

  const fieldRows = [
    { icon: Building2, label: 'Legal Company Name', value: fields.legal_name || item.legal_name },
    { icon: MapPin, label: 'Country of Registration', value: fields.country_code || item.country_code },
    { icon: FileText, label: 'Tax ID', value: fields.tax_id_type ? `${fields.tax_id_type} — ${fields.tax_id}` : '—' },
    { icon: FileSearch, label: 'Registration ID', value: fields.registration_id || 'Not provided' },
    { icon: MapPin, label: 'Registered Address', value: fields.registered_address || '—' },
    { icon: User, label: 'Contact', value: fields.contact_name ? `${fields.contact_name} · ${fields.contact_email}` : '—' },
    { icon: Briefcase, label: 'Supplier Category', value: fields.supplier_category || item.supplier_category },
    { icon: FileText, label: 'Business Justification', value: fields.business_justification || '—' },
    { icon: CreditCard, label: 'Bank Account Holder', value: fields.bank_account_holder || '—' },
    { icon: CreditCard, label: 'Bank Country / Routing', value: fields.bank_country ? `${fields.bank_country} · ${fields.bank_code}` : '—' },
  ];

  return (
    <>
      {/* Backdrop */}
      <div className="panel-backdrop" onClick={onClose} />

      {/* Side Panel */}
      <div className="detail-side-panel">
        {/* Panel Header */}
        <div className="side-panel-header">
          <div>
            <div className="side-panel-eyebrow">VENDOR APPLICATION #{item.id}</div>
            <h2 className="side-panel-title">{item.legal_name}</h2>
            <div className="side-panel-meta">
              <StatusPill status={item.status} />
              <RiskBadge score={item.ai_risk_score} level={item.ai_risk_level} />
              <span className="side-panel-date">{new Date(item.submitted_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</span>
            </div>
          </div>
          <button className="side-panel-close" onClick={onClose}><X size={20}/></button>
        </div>

        <div className="side-panel-body">

          {/* Manual Review Action Bar */}
          {isManual && (
            <div className="manual-action-bar">
              <div className="manual-action-label">
                <AlertCircle size={16} style={{ color: '#d9801e' }} />
                <span>Admin decision required</span>
              </div>
              <div className="manual-action-buttons">
                <button className="btn-approve-lg" disabled={busy} onClick={() => onDecide(item.id, 'APPROVED')}>
                  <CheckCircle2 size={15}/> Approve
                </button>
                <button className="btn-reject-lg" disabled={busy} onClick={() => onDecide(item.id, 'REJECTED')}>
                  <XCircle size={15}/> Reject
                </button>
              </div>
            </div>
          )}

          {/* 5-Stage Pipeline Visualization */}
          <div className="side-section">
            <div className="side-section-title"><Sparkles size={15}/> Verification Pipeline</div>
            <PipelineVisualization
              reviews={item.reviews || []}
              pipelineStages={item.pipeline_stages || []}
              riskScore={item.ai_risk_score}
              riskLevel={item.ai_risk_level}
              riskReason={item.ai_risk_reason}
              status={item.status}
            />
          </div>

          {/* Email Preview */}
          <div className="side-section email-preview-section">
            <div className="side-section-title"><Mail size={15}/> Email Notification</div>
            <EmailPreview communications={item.communications || []} />
          </div>

          {/* Vendor Form Fields */}
          <div className="side-section">
            <div className="side-section-title"><FileText size={15}/> Submitted Form Fields</div>
            <div className="field-grid-panel">
              {fieldRows.map(({ icon: Icon, label, value }) => (
                <div className="field-row-panel" key={label}>
                  <div className="field-row-icon"><Icon size={13}/></div>
                  <div>
                    <div className="field-row-label">{label}</div>
                    <div className="field-row-value">{value}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Uploaded Documents */}
          <div className="side-section">
            <div className="side-section-title"><FileSearch size={15}/> Uploaded Documents & Extracted Fields</div>
            {item.documents && item.documents.length > 0 ? (
              item.documents.map((doc) => {
                const extractedFields = doc.extracted_fields || {};
                return (
                  <div className="doc-detail-card" key={doc.id}>
                    <div className="doc-detail-header">
                      <div className="doc-detail-icon"><FileText size={15}/></div>
                      <div>
                        <div className="doc-detail-name">{doc.original_filename}</div>
                        <div className="doc-detail-type">{doc.document_type?.replaceAll('_', ' ')} · {doc.extraction_method}</div>
                      </div>
                    </div>
                    {Object.keys(extractedFields).length > 0 ? (
                      <div className="doc-extracted-fields">
                        <div className="doc-extracted-label">Extracted Fields</div>
                        {Object.entries(extractedFields).map(([k, v]) => (
                          <div className="doc-field-row" key={k}>
                            <span className="doc-field-key">{k.replaceAll('_', ' ')}</span>
                            <span className="doc-field-val">{String(v)}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="doc-no-fields">No structured fields extracted</div>
                    )}
                  </div>
                );
              })
            ) : (
              <div className="empty-inline"><FileText size={16}/> No documents uploaded.</div>
            )}
          </div>

        </div>
      </div>
    </>
  );
}


// ── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [form, setForm] = useState(initialForm);
  const [files, setFiles] = useState({});
  const [submissions, setSubmissions] = useState([]);
  const [stats, setStats] = useState(null);
  const [selected, setSelected] = useState(null);
  const [audit, setAudit] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [tab, setTab] = useState('overview');       // 'overview' | 'new' | 'audit' | 'admin'
  const [search, setSearch] = useState('');
  const [adminSearch, setAdminSearch] = useState('');
  const [health, setHealth] = useState(null);
  const [adminData, setAdminData] = useState(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [detailItem, setDetailItem] = useState(null);  // item shown in side panel

  // ── Data fetching ────────────────────────────────────────────
  const refresh = async (keepId = selected?.id) => {
    setLoading(true);
    try {
      const [items, totals, service] = await Promise.all([api.list(), api.stats(), api.health()]);
      setSubmissions(items); setStats(totals); setHealth(service);
      if (keepId) {
        const found = items.find((item) => item.id === keepId);
        if (found) setSelected(found);
      }
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };

  const refreshAdmin = useCallback(async () => {
    setAdminLoading(true);
    try { setAdminData(await api.adminStats()); }
    catch (e) { setError(e.message); }
    finally { setAdminLoading(false); }
  }, []);

  useEffect(() => { refresh(null); }, []);
  useEffect(() => { if (tab === 'admin') refreshAdmin(); }, [tab, refreshAdmin]);

  const filtered = useMemo(() => submissions.filter((s) =>
    `${s.legal_name} ${s.contact_email} ${s.country_code} ${s.status}`.toLowerCase().includes(search.toLowerCase())
  ), [submissions, search]);

  const adminFiltered = useMemo(() => (adminData?.submissions || [])
    .filter((s) => s.status === 'PENDING' || s.status === 'SUBMITTED')
    .filter((s) =>
      `${s.legal_name} ${s.supplier_category} ${s.country_code}`.toLowerCase().includes(adminSearch.toLowerCase())
    ), [adminData, adminSearch]);

  const manualQueue = useMemo(() => adminData?.manual_review_queue || [], [adminData]);

  // ── Form helpers ─────────────────────────────────────────────
  const updateField = (key, value) => setForm((old) => ({ ...old, [key]: value }));
  const changeCountry = (value) => {
    const nextTax = taxOptions[value]?.[0]?.value || 'TAX_ID';
    setForm((old) => ({ ...old, country_code: value, bank_country: value, tax_id_type: nextTax }));
  };

  const loadSample = async (variant = 1) => {
    try {
      const sample = await api.sample(variant);
      setForm(sample);
      setNotice(variant === 2
        ? 'Demo data 2 (BluePeak) loaded. Upload the BluePeak certificate of incorporation, GST certificate and cancelled cheque to pass all checks.'
        : 'Demo supplier details loaded. Add the three documents below or submit without them.');
      setError('');
    } catch (e) { setError(e.message); }
  };

  const submit = async (event) => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      let created = await api.create(form);
      for (const slot of documentSlots) {
        if (files[slot.key]) created = await api.upload(created.id, files[slot.key], slot.key);
      }
      created = await api.evaluate(created.id);
      setSelected(created); setTab('overview'); setForm(initialForm); setFiles({});
      setNotice(`Application #${created.id} evaluated by Gemini AI: ${created.status}.`);
      await refresh(created.id);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const evaluate = async (id) => {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.evaluate(id);
      setSelected(result);
      setNotice(`Application #${id} evaluated: ${result.status}.`);
      await refresh(id);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const selectSubmission = async (item) => {
    setSelected(item); setTab('overview'); setError('');
    try { setSelected(await api.get(item.id)); setAudit(await api.audit(item.id)); } catch (e) { setError(e.message); }
  };

  const uploadMore = async (event, type) => {
    const file = event.target.files?.[0]; if (!file || !selected) return;
    setBusy(true); setError('');
    try {
      await api.upload(selected.id, file, type);
      const refreshed = await api.get(selected.id); setSelected(refreshed); await refresh(selected.id);
      setNotice(`${file.name} uploaded. Re-run evaluation to refresh the decision.`);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); event.target.value = ''; }
  };

  const loadAudit = async () => {
    if (!selected) return;
    try { setAudit(await api.audit(selected.id)); setTab('audit'); } catch (e) { setError(e.message); }
  };

  const copyDraft = async (text) => {
    try { await navigator.clipboard.writeText(text); setNotice('Communication draft copied to clipboard.'); }
    catch { setError('Clipboard access was not available in this browser.'); }
  };

  const adminDecide = async (id, decision) => {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.adminDecision(id, decision);
      setNotice(`Application #${id} ${decision.toLowerCase()} by admin.${result.email_sent ? ' Email sent to vendor.' : ''}`);
      setDetailItem(null);
      await Promise.all([refresh(null), refreshAdmin()]);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const openDetail = (item) => setDetailItem(item);
  const closeDetail = () => setDetailItem(null);

  // ── Render ───────────────────────────────────────────────────
  return <div className="app-shell">
    {/* ── Detail Side Panel ── */}
    <DetailPanel item={detailItem} onClose={closeDetail} onDecide={adminDecide} busy={busy} />

    {/* ── Sidebar ── */}
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Building2 size={20}/></div><div><strong>VendorIQ</strong><span>ONBOARDING CONTROL</span></div></div>
      <div className="side-label">WORKSPACE</div>
      <button
        className={`nav-item ${tab !== 'new' && tab !== 'admin' ? 'active' : ''}`}
        onClick={() => { setTab('overview'); setSelected(null); }}
      ><ClipboardList size={17}/> Applications</button>
      {/* Admin Dashboard — nested under Applications */}
      <button
        className={`admin-nav-btn ${tab === 'admin' ? 'active' : ''}`}
        onClick={() => setTab('admin')}
      ><LayoutDashboard size={15}/> Admin Dashboard
        {manualQueue.length > 0 && <span className="manual-queue-badge">{manualQueue.length}</span>}
      </button>
      <button
        className={`nav-item ${tab === 'new' ? 'active' : ''}`}
        onClick={() => { setTab('new'); setSelected(null); }}
      ><Plus size={17}/> New submission</button>
      <div className="sidebar-bottom">
        <div className="sidebar-note"><ShieldAlert size={17}/><div><b>Gemini AI Decisioning</b><p>AI reads all form fields and documents to auto-accept, reject, or flag for manual review.</p></div></div>
        <div className="service-state"><span className="pulse-dot"/> API {health?.status === 'ok' ? 'connected' : 'checking'}</div>
      </div>
    </aside>

    {/* ── Main area ── */}
    <main className="main-area">
      <header className="topbar">
        <div>
          <div className="eyebrow">OPERATIONS / SUPPLIER MANAGEMENT</div>
          <h1>{tab === 'new' ? 'New vendor submission' : tab === 'admin' ? 'Admin Dashboard' : selected ? `Application #${selected.id}` : 'Vendor applications'}</h1>
        </div>
        <div className="topbar-right">
          {health?.gemini_enabled
            ? <span className="gemini-badge"><Sparkles size={11}/> Gemini AI</span>
            : <span className="gemini-badge gemini-off">AI scoring off</span>
          }
          <span className="mode-chip">{health?.verification_mode === 'demo' ? 'Demo' : 'Manual'} verification</span>
          <button className="icon-button" onClick={() => { refresh(); if (tab === 'admin') refreshAdmin(); }} title="Refresh"><RefreshCw size={17}/></button>
          <div className="avatar">OP</div>
        </div>
      </header>

      {error && <div className="alert error"><AlertTriangle size={17}/><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="alert success"><CheckCircle2 size={17}/><span>{notice}</span><button onClick={() => setNotice('')}>×</button></div>}

      {/* ── New submission form ── */}
      {tab === 'new' ? <section className="content-grid new-view">
        <form className="panel submission-form" onSubmit={submit}>
          <div className="panel-heading"><div><span className="section-kicker">SUPPLIER INTAKE</span><h2>Vendor details</h2><p>Capture the legal entity, tax identity, business purpose and payment destination.</p></div><div className="sample-buttons"><button type="button" className="button secondary small" onClick={()=>loadSample(1)}>Load demo data 1</button><button type="button" className="button secondary small" onClick={()=>loadSample(2)} title="BluePeak Industrial Solutions — matches the BluePeak documents">Load demo data 2</button></div></div>
          <div className="form-section-title"><span>01</span> Company information</div>
          <div className="form-grid">
            <Field label="Legal company name" required><input required value={form.legal_name} onChange={e=>updateField('legal_name',e.target.value)} placeholder="e.g. Northstar Components Ltd"/></Field>
            <Field label="Country of registration" required><select value={form.country_code} onChange={e=>changeCountry(e.target.value)}><option value="IN">India</option><option value="US">United States</option><option value="GB">United Kingdom</option><option value="AU">Australia</option><option value="SG">Singapore</option></select></Field>
            <Field label="Tax identifier type" required><select value={form.tax_id_type} onChange={e=>updateField('tax_id_type',e.target.value)}>{(taxOptions[form.country_code]||[{value:'TAX_ID',label:'Tax ID'}]).map(x=><option key={x.value} value={x.value}>{x.label}</option>)}</select></Field>
            <Field label="Tax identifier" required><input required value={form.tax_id} onChange={e=>updateField('tax_id',e.target.value)} placeholder="Enter tax registration number"/></Field>
            <Field label="Company registration ID"><input value={form.registration_id} onChange={e=>updateField('registration_id',e.target.value)} placeholder="CIN / company number"/></Field>
            <Field label="Supplier category" required><select required value={form.supplier_category} onChange={e=>updateField('supplier_category',e.target.value)}><option value="">Select category</option><option>IT services</option><option>Industrial supplies</option><option>Logistics</option><option>Professional services</option><option>Facilities</option><option>Other</option></select></Field>
            <div className="span-2"><Field label="Registered business address" required><textarea required rows="2" value={form.registered_address} onChange={e=>updateField('registered_address',e.target.value)} placeholder="Registered address as shown on official records"/></Field></div>
            <div className="span-2"><Field label="Business justification" required hint="Why is this supplier being onboarded? Gemini AI evaluates this for credibility."><textarea required rows="2" value={form.business_justification} onChange={e=>updateField('business_justification',e.target.value)} placeholder="Describe the business need for engaging this supplier"/></Field></div>
          </div>
          <div className="form-section-title"><span>02</span> Primary contact</div>
          <div className="form-grid">
            <Field label="Contact name" required><input required value={form.contact_name} onChange={e=>updateField('contact_name',e.target.value)} placeholder="Supplier representative"/></Field>
            <Field label="Business email" required><input type="email" required value={form.contact_email} onChange={e=>updateField('contact_email',e.target.value)} placeholder="name@company.com"/></Field>
          </div>
          <div className="form-section-title"><span>03</span> Payment details</div>
          <div className="form-grid">
            <Field label="Bank account holder" required hint="Must correspond to the legal entity or be independently explained."><input required value={form.bank_account_holder} onChange={e=>updateField('bank_account_holder',e.target.value)} placeholder="Name as held by bank"/></Field>
            <Field label="Bank country" required><select value={form.bank_country} onChange={e=>updateField('bank_country',e.target.value)}><option value="IN">India</option><option value="US">United States</option><option value="GB">United Kingdom</option><option value="AU">Australia</option><option value="SG">Singapore</option></select></Field>
            <Field label="Bank routing code" required hint="IFSC / routing number / sort code / BSB"><input required value={form.bank_code} onChange={e=>updateField('bank_code',e.target.value)} placeholder="Bank routing code"/></Field>
            <Field label="Verification reference" required hint="Provider token/reference only; do not enter a full account number."><input required value={form.bank_reference} onChange={e=>updateField('bank_reference',e.target.value)} placeholder="Bank verification reference"/></Field>
          </div>
          <div className="form-section-title"><span>04</span> Supporting documents</div>
          <p className="subtle-copy">Upload the supporting evidence. Gemini AI reads these to verify consistency with your form fields.</p>
          <div className="upload-grid">
            {documentSlots.map(slot=><div className="upload-slot" key={slot.key}><div className="upload-icon"><FileText size={18}/></div><div className="upload-meta"><b>{slot.label}</b><small>{files[slot.key]?.name||slot.hint}</small></div><label className="file-button">{files[slot.key]?'Change':'Choose'}<input type="file" accept=".pdf,.png,.jpg,.jpeg,.tiff,.bmp,.txt,.csv" onChange={e=>setFiles(old=>({...old,[slot.key]:e.target.files?.[0]}))}/></label></div>)}
          </div>
          <div className="form-footer"><span><Sparkles size={15}/> Gemini AI evaluates all fields and documents to auto-decide.</span><button className="button primary" disabled={busy}>{busy?'Processing…':'Submit & evaluate'} <ArrowRight size={16}/></button></div>
        </form>
        <aside className="side-explainer"><div className="explainer-card"><div className="explainer-icon"><Brain size={20}/></div><h3>How Gemini decides</h3><ol><li><b>Document intelligence</b><span>Extract fields and retain source evidence.</span></li><li><b>AI risk scoring</b><span>Gemini reads all form fields AND document content.</span></li><li><b>Auto-decisioning</b><span>Score ≤30 → Approved · 31–69 → Manual review · ≥70 → Rejected</span></li><li><b>Automatic email</b><span>Vendor is notified instantly on approve or reject.</span></li></ol></div><div className="notice-card"><ShieldAlert size={17}/><p><b>Demo environment</b><br/>Local verification simulates a successful format check. It does not query government or banking registries.</p></div></aside>
      </section>

      /* ── Admin Dashboard ── */
      : tab === 'admin' ? <section className="content-area">
        <div className="stats-grid">
          <StatCard icon={Building2} label="Total applications" value={adminData?.total} tint="blue"/>
          <StatCard icon={Clock3} label="Manual review" value={adminData?.by_status?.PENDING || 0} tint="amber"/>
          <StatCard icon={BadgeCheck} label="Approved" value={adminData?.by_status?.APPROVED || 0} tint="green"/>
          <StatCard icon={XCircle} label="Rejected" value={adminData?.by_status?.REJECTED || 0} tint="violet"/>
        </div>

        {/* ── MANUAL REVIEW QUEUE ── */}
        {manualQueue.length > 0 && (
          <div className="manual-review-queue-banner">
            <div className="mrq-header">
              <div className="mrq-title-row">
                <AlertTriangle size={18} className="mrq-icon"/>
                <div>
                  <div className="mrq-title">Manual Review Required</div>
                  <div className="mrq-subtitle">These {manualQueue.length} application{manualQueue.length > 1 ? 's' : ''} scored in the medium-risk range (31–69). Gemini could not auto-decide — your review is needed.</div>
                </div>
              </div>
            </div>
            <div className="mrq-cards">
              {manualQueue.map(item => (
                <div className="mrq-card" key={item.id}>
                  <div className="mrq-card-top">
                    <div className="mrq-vendor-name">{item.legal_name}</div>
                    <RiskBadge score={item.ai_risk_score} level={item.ai_risk_level}/>
                  </div>
                  <div className="mrq-reason">{item.ai_risk_reason || 'Awaiting Gemini evaluation'}</div>
                  <div className="mrq-card-footer">
                    <span className="mrq-meta">#{item.id} · {item.supplier_category} · {item.country_code}</span>
                    <div className="mrq-actions">
                      <button className="btn-view-detail" onClick={() => openDetail(item)}><Eye size={12}/> Review</button>
                      <button className="btn-approve" disabled={busy} onClick={() => adminDecide(item.id, 'APPROVED')}><CheckCircle2 size={12}/> Approve</button>
                      <button className="btn-reject" disabled={busy} onClick={() => adminDecide(item.id, 'REJECTED')}><XCircle size={12}/> Reject</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="admin-grid">
          {/* Main table */}
          <div className="panel list-panel">
            <div className="list-toolbar">
              <div><span className="section-kicker">NEEDS REVIEW</span><h2>Applications pending review</h2></div>
              <div className="toolbar-actions">
                <div className="search-box"><Search size={15}/><input value={adminSearch} onChange={e=>setAdminSearch(e.target.value)} placeholder="Search…"/></div>
                <button className="button secondary small" onClick={refreshAdmin} disabled={adminLoading}><RefreshCw size={14}/> Refresh</button>
              </div>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Supplier</th>
                    <th>Category</th>
                    <th>AI Risk</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {adminFiltered.map(item => (
                    <tr key={item.id}>
                      <td>
                        <div className="supplier-cell">
                          <div className="supplier-avatar">{item.legal_name.slice(0,1).toUpperCase()}</div>
                          <div>
                            <b>{item.legal_name}</b>
                            <small>{item.country_code} · {new Date(item.submitted_at).toLocaleDateString()}</small>
                          </div>
                        </div>
                      </td>
                      <td><span className="country-tag">{item.supplier_category}</span></td>
                      <td><RiskBadge score={item.ai_risk_score} level={item.ai_risk_level}/></td>
                      <td><StatusPill status={item.status}/></td>
                      <td>
                        <div className="admin-action-cell">
                          <button className="btn-view-detail" onClick={() => openDetail(item)}>
                            <Eye size={12}/> Details
                          </button>
                          <button className="btn-approve" disabled={busy} onClick={() => adminDecide(item.id, 'APPROVED')}><CheckCircle2 size={12}/> Approve</button>
                          <button className="btn-reject" disabled={busy} onClick={() => adminDecide(item.id, 'REJECTED')}><XCircle size={12}/> Reject</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!adminFiltered.length && <tr><td colSpan="5"><div className="empty-state"><FileText size={22}/><b>{adminLoading ? 'Loading…' : 'No applications need review'}</b><span>All applications have been auto-decided by Gemini AI.</span></div></td></tr>}
                </tbody>
              </table>
            </div>
            <div className="table-footer"><span>Showing {adminFiltered.length} application{adminFiltered.length !== 1 ? 's' : ''} needing review</span></div>
          </div>

          {/* Right sidebar — Communications */}
          <aside className="admin-side">
            <div className="panel comms-panel">
              <h3><Mail size={15} style={{color:'#3c6ccc'}}/> Email Communications</h3>
              {(adminData?.submissions || []).filter(s => s.latest_communication).length ? (
                <div>
                  {(adminData?.submissions || [])
                    .filter(s => s.latest_communication)
                    .sort((a, b) => new Date(b.latest_communication.created_at) - new Date(a.latest_communication.created_at))
                    .slice(0, 5)
                    .map(item => (
                      <div key={item.id} className="comm-item">
                        <div className={`comm-icon ${item.latest_communication.sent ? 'sent' : ''}`}><Mail size={14}/></div>
                        <div>
                          <b title={item.latest_communication.subject}>{item.latest_communication.subject}</b>
                          <small>{item.legal_name} · {new Date(item.latest_communication.created_at).toLocaleDateString()}</small>
                          <span className={`email-sent-tag ${item.latest_communication.sent ? 'sent' : 'draft'}`}>
                            {item.latest_communication.sent ? '✓ Sent' : 'Draft only'}
                          </span>
                          <button className="text-button" style={{marginTop:4,display:'block'}} onClick={() => copyDraft(item.latest_communication.body)}>
                            <Copy size={11}/> Copy body
                          </button>
                        </div>
                      </div>
                    ))}
                </div>
              ) : <div className="empty-inline"><Mail size={16}/> No communications yet.</div>}
            </div>
          </aside>
        </div>
      </section>

      /* ── Applications list / detail ── */
      : <section className="content-area">
        {!selected ? <>
          <div className="stats-grid">
            <StatCard icon={Building2} label="Total applications" value={stats?.total} tint="blue"/>
            <StatCard icon={Clock3} label="Pending review" value={stats?.by_status?.PENDING||0} tint="amber"/>
            <StatCard icon={BadgeCheck} label="Approved vendors" value={stats?.by_status?.APPROVED||0} tint="green"/>
            <StatCard icon={FileCheck2} label="Documents processed" value={stats?.documents} tint="violet"/>
          </div>
          <div className="panel list-panel">
            <div className="list-toolbar"><div><span className="section-kicker">ONBOARDING QUEUE</span><h2>Applications</h2></div><div className="toolbar-actions"><div className="search-box"><Search size={16}/><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search vendors…"/></div><button className="button primary" onClick={()=>setTab('new')}><Plus size={16}/> New vendor</button></div></div>
            <div className="table-wrap"><table><thead><tr><th>Supplier</th><th>Country</th><th>Submitted</th><th>AI Risk</th><th>Status</th><th>Documents</th><th></th></tr></thead><tbody>
              {filtered.map(item=><tr key={item.id} onClick={()=>selectSubmission(item)} className="click-row"><td><div className="supplier-cell"><div className="supplier-avatar">{item.legal_name.slice(0,1).toUpperCase()}</div><div><b>{item.legal_name}</b><small>{item.contact_email}</small></div></div></td><td><span className="country-tag">{item.country_code}</span></td><td>{new Date(item.submitted_at).toLocaleDateString()}</td><td><RiskBadge score={item.ai_risk_score} level={item.ai_risk_level}/></td><td><StatusPill status={item.status}/></td><td>{item.documents?.length||0} files</td><td><ChevronRight size={17} className="muted-icon"/></td></tr>)}
              {!filtered.length&&<tr><td colSpan="7"><div className="empty-state"><FileText size={24}/><b>{loading?'Loading applications…':'No applications found'}</b><span>Create a vendor submission to begin.</span></div></td></tr>}
            </tbody></table></div>
            <div className="table-footer"><span>Showing {filtered.length} of {submissions.length} applications</span><button className="text-button" onClick={()=>refresh()}><RefreshCw size={14}/> Refresh queue</button></div>
          </div>
        </> : <>
          <div className="detail-top"><button className="back-link" onClick={()=>setSelected(null)}>← All applications</button><div className="detail-actions"><RiskBadge score={selected.ai_risk_score} level={selected.ai_risk_level}/><StatusPill status={selected.status}/>{selected.status!=='APPROVED'&&<button className="button primary" disabled={busy} onClick={()=>evaluate(selected.id)}><RefreshCw size={15}/> {busy?'Evaluating…':'Run checks'}</button>}</div></div>
          <div className="detail-grid">
            <div className="detail-main">
              <div className="panel vendor-summary"><div className="vendor-summary-head"><div className="large-avatar">{selected.legal_name.slice(0,1).toUpperCase()}</div><div className="vendor-title"><span className="section-kicker">VENDOR PROFILE · #{selected.id}</span><h2>{selected.legal_name}</h2><p>{selected.supplier_category} · {selected.country_code} · {selected.contact_email}</p></div></div><div className="summary-meta"><div><small>TAX ID</small><b>{selected.tax_id_type} · {selected.tax_id}</b></div><div><small>SUBMITTED</small><b>{new Date(selected.submitted_at).toLocaleString()}</b></div><div><small>DOCUMENTS</small><b>{selected.documents?.length||0} uploaded</b></div></div></div>

              {/* Verification pipeline: shows every stage and opens the one that stopped the application */}
              {selected.reviews?.length > 0 && (
                <div className="panel reviews-panel">
                  <div className="panel-heading"><div><span className="section-kicker">VERIFICATION RESULT</span><h2>Pipeline</h2><p>Click any stage to see its findings and required actions.</p></div></div>
                  <PipelineVisualization
                    reviews={selected.reviews}
                    riskScore={selected.ai_risk_score}
                    riskLevel={selected.ai_risk_level}
                    status={selected.status}
                  />
                  {selected.ai_detail_notes && (
                    <div className="ai-detail-block">
                      <div className="ai-detail-block-title"><Brain size={13}/> Gemini detailed analysis</div>
                      <p>{selected.ai_detail_notes}</p>
                    </div>
                  )}
                </div>
              )}

              <div className="panel documents-panel"><div className="panel-heading"><div><span className="section-kicker">SUBMITTED EVIDENCE</span><h2>Documents</h2></div></div><div className="document-list">{selected.documents?.map(doc=><div className="document-row" key={doc.id}><div className="document-icon"><FileText size={17}/></div><div className="document-name"><b>{doc.original_filename}</b><small>{doc.document_type.replaceAll('_',' ')} · {doc.extraction_method}</small></div><span className="doc-state">Processed</span></div>)}{!selected.documents?.length&&<span className="subtle-copy">No documents uploaded.</span>}</div>
                {selected.status!=='APPROVED'&&<div className="add-doc-row">{documentSlots.map(slot=><label className="button secondary small" key={slot.key}><UploadCloud size={14}/> Add {slot.label.split(' ')[0]}<input type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.tiff,.bmp,.txt,.csv" onChange={e=>uploadMore(e,slot.key)}/></label>)}</div>}
              </div>
            </div>
            <aside className="detail-side">
              <div className="panel decision-card"><span className="section-kicker">FINAL DECISION</span><div className={`decision-symbol ${selected.status?.toLowerCase()}`}>{selected.status==='APPROVED'?<CheckCircle2 size={25}/>:selected.status==='REJECTED'?<XCircle size={25}/>:<Clock3 size={25}/>}</div><h2>{selected.status}</h2>{(()=>{const h=decisionHeadline(selected.status, pipelineOutcome(selected.reviews));return <div className="decision-reason"><b>{h.title}</b><p>{h.detail}</p></div>;})()}{selected.demo_mode&&<div className="demo-warning"><ShieldAlert size={14}/> Demo checks are not authoritative registry/bank verification.</div>}</div>
              {selected.communications?.length>0&&(()=>{
                const comm = selected.communications[selected.communications.length-1];
                const isHtml = comm.body && comm.body.trim().startsWith('<');
                return (
                  <div className="panel communication-card">
                    <div className="panel-heading"><div><span className="section-kicker">EMAIL NOTIFICATION</span><h3><Mail size={16}/> {comm.sent ? 'Email sent ✓' : 'Draft (not sent)'}</h3></div></div>
                    <div className="draft-to">To: {comm.recipient}</div>
                    <b className="draft-subject">{comm.subject}</b>
                    <div style={{margin:'10px 0',border:'1px solid #e2e8f0',borderRadius:8,overflow:'hidden',background:'#f8fafc'}}>
                      <div style={{display:'flex',alignItems:'center',gap:8,padding:'6px 10px',background:'#e2e8f0',borderBottom:'1px solid #cbd5e1'}}>
                        <div style={{display:'flex',gap:4}}><span style={{width:8,height:8,borderRadius:'50%',background:'#ef4444',display:'inline-block'}}/><span style={{width:8,height:8,borderRadius:'50%',background:'#f59e0b',display:'inline-block'}}/><span style={{width:8,height:8,borderRadius:'50%',background:'#10b981',display:'inline-block'}}/></div>
                        <span style={{fontSize:10,fontFamily:'monospace',color:'#64748b',background:'#fff',padding:'1px 8px',borderRadius:4,flex:1,maxWidth:180,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>mail.vendoriq.local</span>
                      </div>
                      {isHtml
                        ? <iframe sandbox="allow-same-origin" srcDoc={comm.body} title="Email Preview" style={{width:'100%',height:260,border:0,display:'block',background:'#fff'}}/>
                        : <pre style={{margin:0,padding:'12px',fontSize:11,lineHeight:1.6,color:'#334155',whiteSpace:'pre-wrap',wordBreak:'break-word',maxHeight:260,overflow:'auto',background:'#fff'}}>{comm.body}</pre>
                      }
                    </div>
                    <button className="button secondary full" onClick={()=>copyDraft(comm.body)}><Copy size={14}/> Copy email body</button>
                  </div>
                );
              })()}
              <div className="panel profile-card"><span className="section-kicker">SUBMITTED PROFILE</span><dl><dt>Registered address</dt><dd>{selected.registered_address}</dd><dt>Contact</dt><dd>{selected.contact_name} · {selected.contact_email}</dd><dt>Bank account holder</dt><dd>{selected.bank_account_holder}</dd><dt>Bank country / code</dt><dd>{selected.bank_country} · {selected.bank_code}</dd><dt>Business purpose</dt><dd>{selected.business_justification}</dd></dl></div>
              <button className="button secondary full" onClick={loadAudit}><Activity size={15}/> View audit history</button>
              {tab==='audit'&&<div className="panel audit-panel"><div className="panel-heading"><h3>Audit history</h3></div>{audit.map(row=><div className="audit-row" key={row.id}><b>{row.action.replaceAll('_',' ')}</b><small>{row.actor} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div>}
            </aside>
          </div>
        </>}
      </section>}

      <footer className="app-footer"><span>VendorIQ · Gemini AI Decisioning</span><span>Score ≤30 Auto-Approve · 31–69 Manual Review · ≥70 Auto-Reject · Emails sent automatically</span></footer>
    </main>
  </div>;
}
